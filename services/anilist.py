"""Async AniList GraphQL client.

Responsibilities:

* Non-blocking HTTP via ``aiohttp``.
* Client-side rate limiting (sliding window) so the bot stays under AniList's
  limit even with many linked users.
* Automatic retries with backoff for transient failures, honoring
  ``Retry-After`` on HTTP 429.
* A short-lived in-memory cache so repeated manual checks don't burn API calls.
* Typed activity models so the rest of the codebase never touches raw JSON.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://graphql.anilist.co"

# AniList's public API limit is 90 req/min but is currently degraded to
# 30 req/min; stay comfortably under it.
DEFAULT_REQUESTS_PER_MINUTE = 25
DEFAULT_CACHE_TTL_SECONDS = 45.0
DEFAULT_MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 15


class AniListError(RuntimeError):
    """Base error for AniList API failures."""


class AniListUserNotFound(AniListError):
    """Raised when a username does not exist on AniList."""


@dataclass(frozen=True, slots=True)
class AniListUser:
    id: int
    name: str
    site_url: str
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class ListActivity:
    """A watching/reading progress update (anime or manga list activity)."""

    id: int
    created_at: int
    status: str
    progress: str | None
    media_title: str
    media_url: str | None
    media_type: str | None  # "ANIME" | "MANGA"
    cover_image_url: str | None
    site_url: str | None
    user: AniListUser | None


@dataclass(frozen=True, slots=True)
class TextActivity:
    """A free-form status post by the user."""

    id: int
    created_at: int
    text: str
    site_url: str | None
    user: AniListUser | None


Activity = ListActivity | TextActivity


USER_QUERY = """
query ($name: String) {
  User(name: $name) {
    id
    name
    siteUrl
    avatar { large }
  }
}
"""

ACTIVITIES_QUERY = """
query ($userId: Int, $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    activities(userId: $userId, type_in: [ANIME_LIST, MANGA_LIST, TEXT], sort: ID_DESC) {
      __typename
      ... on ListActivity {
        id
        status
        progress
        createdAt
        siteUrl
        user { id name siteUrl avatar { large } }
        media {
          type
          siteUrl
          title { romaji english }
          coverImage { large }
        }
      }
      ... on TextActivity {
        id
        text(asHtml: false)
        createdAt
        siteUrl
        user { id name siteUrl avatar { large } }
      }
    }
  }
}
"""


def profile_url(username: str) -> str:
    return f"https://anilist.co/user/{username}/"


def _parse_user(raw: dict | None) -> AniListUser | None:
    if not raw:
        return None
    try:
        return AniListUser(
            id=int(raw["id"]),
            name=str(raw["name"]),
            site_url=raw.get("siteUrl") or profile_url(str(raw["name"])),
            avatar_url=(raw.get("avatar") or {}).get("large"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_activity(raw: dict) -> Activity | None:
    """Parse one raw activity; returns None for unknown/malformed entries."""
    typename = raw.get("__typename")
    try:
        if typename == "ListActivity":
            media = raw.get("media") or {}
            title = media.get("title") or {}
            return ListActivity(
                id=int(raw["id"]),
                created_at=int(raw["createdAt"]),
                status=raw.get("status") or "updated",
                progress=raw.get("progress"),
                media_title=title.get("romaji") or title.get("english") or "Unknown title",
                media_url=media.get("siteUrl"),
                media_type=media.get("type"),
                cover_image_url=(media.get("coverImage") or {}).get("large"),
                site_url=raw.get("siteUrl"),
                user=_parse_user(raw.get("user")),
            )
        if typename == "TextActivity":
            return TextActivity(
                id=int(raw["id"]),
                created_at=int(raw["createdAt"]),
                text=raw.get("text") or "",
                site_url=raw.get("siteUrl"),
                user=_parse_user(raw.get("user")),
            )
    except (KeyError, TypeError, ValueError):
        log.warning("Skipping unparseable AniList activity payload: %r", raw)
        return None
    log.debug("Ignoring unsupported activity type: %r", typename)
    return None


class _SlidingWindowRateLimiter:
    """Allows at most ``max_calls`` per ``period`` seconds, sleeping as needed."""

    def __init__(self, max_calls: int, period: float = 60.0) -> None:
        self._max_calls = max_calls
        self._period = period
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._evict(now)
            if len(self._calls) >= self._max_calls:
                wait_for = self._period - (now - self._calls[0])
                if wait_for > 0:
                    log.debug("AniList rate limiter sleeping %.1fs", wait_for)
                    await asyncio.sleep(wait_for)
                self._evict(time.monotonic())
            self._calls.append(time.monotonic())

    def _evict(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self._period:
            self._calls.popleft()


class AniListClient:
    """High-level async client for the AniList GraphQL API."""

    def __init__(
        self,
        *,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._limiter = _SlidingWindowRateLimiter(requests_per_minute)
        self._cache_ttl = cache_ttl
        self._max_retries = max_retries
        # (user_id, per_page) -> (expires_at_monotonic, activities)
        self._activity_cache: dict[tuple[int, int], tuple[float, list[Activity]]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _post(self, query: str, variables: dict) -> dict:
        """Run a GraphQL request with rate limiting and retries."""
        session = await self._get_session()
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            await self._limiter.acquire()
            try:
                async with session.post(
                    API_URL, json={"query": query, "variables": variables}
                ) as response:
                    if response.status == 429:
                        retry_after = _retry_after_seconds(response)
                        log.warning(
                            "AniList rate limit hit (attempt %d); retrying in %.0fs",
                            attempt,
                            retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if response.status >= 500:
                        last_error = AniListError(f"AniList server error {response.status}")
                        log.warning("%s (attempt %d)", last_error, attempt)
                        await asyncio.sleep(min(2**attempt, 8))
                        continue
                    payload = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                log.warning("AniList request failed (attempt %d): %s", attempt, exc)
                await asyncio.sleep(min(2**attempt, 8))
                continue

            errors = payload.get("errors")
            if errors:
                if _is_not_found(errors):
                    raise AniListUserNotFound("AniList returned Not Found for this request.")
                message = errors[0].get("message", "unknown error")
                raise AniListError(f"AniList API error: {message}")
            return payload.get("data") or {}

        raise AniListError(
            f"AniList request failed after {self._max_retries} attempts."
        ) from last_error

    async def resolve_user(self, username: str) -> AniListUser:
        """Validate that ``username`` exists on AniList and return its profile.

        Raises :class:`AniListUserNotFound` if it does not exist.
        """
        data = await self._post(USER_QUERY, {"name": username})
        user = _parse_user(data.get("User"))
        if user is None:
            raise AniListUserNotFound(f"No AniList user named {username!r} was found.")
        return user

    async def fetch_activities(
        self, user_id: int, *, per_page: int = 10, use_cache: bool = True
    ) -> list[Activity]:
        """Fetch the most recent activities for an AniList user, newest first."""
        cache_key = (user_id, per_page)
        now = time.monotonic()
        if use_cache:
            cached = self._activity_cache.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]

        data = await self._post(ACTIVITIES_QUERY, {"userId": user_id, "perPage": per_page})
        raw_activities = (data.get("Page") or {}).get("activities") or []
        activities = [
            parsed for raw in raw_activities if (parsed := _parse_activity(raw)) is not None
        ]
        self._activity_cache[cache_key] = (now + self._cache_ttl, activities)
        self._prune_cache(now)
        return activities

    async def fetch_latest_activity_id(self, user_id: int) -> int | None:
        """Return the newest activity ID for a user, or None if they have none."""
        activities = await self.fetch_activities(user_id, per_page=1, use_cache=False)
        return activities[0].id if activities else None

    def _prune_cache(self, now: float) -> None:
        expired = [key for key, (expires, _) in self._activity_cache.items() if expires <= now]
        for key in expired:
            del self._activity_cache[key]


def _retry_after_seconds(response: aiohttp.ClientResponse) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 5.0


def _is_not_found(errors: list[dict]) -> bool:
    for error in errors:
        if error.get("status") == 404:
            return True
        if "not found" in str(error.get("message", "")).lower():
            return True
    return False
