"""Riot routing: which shard each kind of call goes to.

Riot routes some APIs by platform (a single shard like na1) and others by
region (a cluster like americas). Account and TFT match calls are regional;
summoner, league and status calls are platform. RiotClient picks the shard
here and hands it to Riot Watcher, which builds the final URL.
"""

from __future__ import annotations

# Platform routing values and the region their TFT match data lives in.
PLATFORM_TO_REGION = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "ph2": "sea", "sg2": "sea", "th2": "sea", "tw2": "sea", "vn2": "sea",
}
PLATFORMS = frozenset(PLATFORM_TO_REGION)
REGIONS = frozenset({"americas", "europe", "asia", "sea"})

# account-v1 is served by americas, europe and asia only; sea players look
# their Riot ID up through asia.
ACCOUNT_REGION = {"americas": "americas", "europe": "europe", "asia": "asia", "sea": "asia"}


def account_region(region: str) -> str:
    return ACCOUNT_REGION[region]


def platform_url(platform: str) -> str:
    return f"https://{platform}.api.riotgames.com"


def regional_url(region: str) -> str:
    return f"https://{region}.api.riotgames.com"
