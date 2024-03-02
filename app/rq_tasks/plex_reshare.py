import os
import redis
import random
import string
import requests
import socket
import json
import rq
import re
import datetime
from urllib.parse import urlencode, urlparse

from starlette.config import Config

config = Config()  # env_prefix='APP_'
PLEX_TOKEN = config('PLEX_TOKEN', cast=str, default="")
REDIS_REFRESH_TTL = 3 * 60 * 60
REDIS_PATH_TTL = 48 * 60 * 60
IGNORE_EXTENSIONS = ["avi", None]
IGNORE_RESOLUTIONS = ["sd", None]
IGNORE_MOVIE_TEMPLATES = [r'^\d{2}\s.*\.\w{3,4}$']
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
    " (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
}

r = redis.Redis(
    host=config('REDIS_HOST', default="redis"),
    port=config('REDIS_PORT', cast=int, default=6379),
    db=11, decode_responses=True
)
rq_redis = redis.Redis(
    host=config('REDIS_HOST', default="redis"),
    port=config('REDIS_PORT', cast=int, default=6379),
    db=config('REDIS_DB_RQ', cast=int, default=11),
)
rq_queue = rq.Queue(name='default', connection=rq_redis)


class DynamicAccessNestedDict:
    """Dynamically get/set nested dictionary keys of 'data' dict"""

    def __init__(self, data: dict):
        self.data = data

    def getval(self, keys: list):
        data = self.data
        for k in keys:
            data = data[k]
        return data

    def setval(self, keys: list, val) -> None:
        data = self.data
        lastkey = keys[-1]
        for k in keys[:-1]:  # when assigning drill down to *second* last key
            data = data[k]
        data[lastkey] = val


def _get_servers() -> list[dict]:
    query_params = {
        "includeHttps": 1,
        "includeRelay": 0,
        "includeIPv6": 0,
        "X-Plex-Client-Identifier": "".join(
            random.choices(
                string.ascii_uppercase + string.ascii_lowercase + string.digits, k=24
            )
        ),
        "X-Plex-Platform-Version": "16.6",
        "X-Plex-Token": PLEX_TOKEN,
    }

    req = requests.get(
        url=f"https://clients.plex.tv/api/v2/resources?{urlencode(query_params)}",
        headers=HEADERS,
    )

    servers = {}
    for server in req.json():
        if not server["owned"] and server["provides"] == "server":


            for conn in server["connections"]:
                if not conn["relay"] and not conn["local"] and not conn["IPv6"]:
                    custom_access = False
                    if "plex.direct" not in conn["uri"]:
                        custom_access = True

                        s = [
                            c
                            for c in server["connections"]
                            if "plex.direct" in c["uri"]
                        ]

                        url = urlparse(conn["uri"])
                        server_ip = socket.gethostbyname(url.netloc.split(":")[0])
                        conn[
                            "uri"
                        ] = f"{server_ip.replace('.', '-')}.{s[0]['uri'].split('.')[1]}.plex.direct:{conn['port']}"

                    uri = conn["uri"].split("://")[-1]

                    if not servers.get(server["clientIdentifier"]) or custom_access:
                        servers[server["clientIdentifier"]] = {
                            "node": uri.split(".")[1],
                            "uri": uri,
                            "ip": uri.split(".")[0].replace("-", "."),
                            "port": conn["port"],
                            "token": server["accessToken"],
                        }

    return list(servers.values())


def _get_common_path(paths: list) -> str:
    path_chunks = {}
    for path in [p.strip("/") for p in paths]:
        for p in path.split("/"):
            if not path_chunks.get(p):
                path_chunks[p] = 1
            else:
                path_chunks[p] += 1

    common_path = [p for p, n in path_chunks.items() if n == max(path_chunks.values())]

    return f"{'/'.join(common_path)}"


# set dir structure in redis
def _set_dir_structure(d, parent=""):
    for k, v in d.items():
        key = f"{parent}/{k}".strip("/")

        if isinstance(v, dict):
            if len(list(v.keys())) > 0:
                r.sadd(key, *list(v.keys()))
                r.expire(key, REDIS_PATH_TTL)
            _set_dir_structure(v, key)
        else:
            r.set(key, v)
            r.expire(key, REDIS_PATH_TTL)


def get_plex_servers() -> None:
    rkey = "pr:servers"
    if not r.exists(rkey):
        plex_servers = _get_servers()
        r.set(rkey, json.dumps(plex_servers))
        r.expire(rkey, int(REDIS_REFRESH_TTL / 3))
    else:
        plex_servers = json.loads(r.get(rkey))

    for plex_server in plex_servers:
        rkey_node_refresh = f"pr:node:{plex_server['node']}:refresh"
        rkey_node_ip = f"pr:node:{plex_server['node']}:ip"
        rkey_node_port = f"pr:node:{plex_server['node']}:port"
        rkey_node_token = f"pr:node:{plex_server['node']}:token"

        # no need to refresh
        if r.get(rkey_node_refresh):
            continue

        r.set(rkey_node_refresh, str(datetime.datetime.now()))
        r.expire(
            rkey_node_refresh,
            REDIS_REFRESH_TTL + random.randint(6, 24) * 60 * 60,
        )

        r.set(rkey_node_ip, plex_server["ip"])
        r.set(rkey_node_port, str(plex_server["port"]))
        r.set(rkey_node_token, plex_server["token"])

        rq_queue.enqueue('rq_tasks.get_plex_libraries', retry=rq.Retry(max=3, interval=[10, 30, 60]), plex_server=plex_server)
        rq_queue.enqueue_in(datetime.timedelta(hours=3),
            'rq_tasks.get_plex_servers', retry=rq.Retry(max=3, interval=[10, 30, 60]))

def get_plex_libraries(plex_server: dict = None) -> None:
    query_params = {"X-Plex-Token": plex_server["token"]}
    libraries = requests.get(
        url=f"https://{plex_server['uri']}/library/sections?{urlencode(query_params)}",
        timeout=15,
        headers=HEADERS,
    )

    for library in libraries.json()["MediaContainer"]["Directory"]:
        if library["type"] in ["movie", "show"]:
            rq_queue.enqueue('rq_tasks.get_plex_library', retry=rq.Retry(max=3, interval=[10, 30, 60]),
                plex_server = plex_server, library = library, offset = 0)

def get_plex_library(plex_server: dict = None, library: dict = None, offset: int = None, ) -> None:
    query_params = {
        "X-Plex-Token": plex_server["token"],
        "X-Plex-Container-Start": offset,
        "X-Plex-Container-Size": 100,
    }

    library_res = requests.get(
        url=f"https://{plex_server['uri']}/library/sections/{library['key']}/all?{urlencode(query_params)}",
        headers=HEADERS,
    )

    media_container = library_res.json()["MediaContainer"]
    rq_queue.enqueue(f"rq_tasks.get_{library['type']}s", media_container=media_container, plex_server=plex_server)

    if (
        media_container["size"] + media_container["offset"]
        < media_container["totalSize"]
    ):
        offset += 100
        rq_queue.enqueue_in(datetime.timedelta(seconds=random.randint(5,60)),
            'rq_tasks.get_plex_library', retry=rq.Retry(max=3, interval=[10, 30, 60]),
            plex_server = plex_server, library = library, offset = offset)

def get_movies(media_container: dict = None, plex_server: dict = None) -> None:
    movies_list = {}

    for movie in media_container["Metadata"]:
        for media in movie["Media"]:
            if media.get("videoResolution") in IGNORE_RESOLUTIONS:
                continue

            for part in media["Part"]:
                movie_key = part.get("key")
                movie_name = part.get("file")

                if not movie_key or not movie_name:
                    continue

                if part.get("container") in IGNORE_EXTENSIONS:
                    continue

                if part["size"] / 1000000 < 200:
                    continue

                movie_file = movie_name.split("/")[-1]

                # ignore file that match a specific name-template
                ignore_file = False
                for imt in IGNORE_MOVIE_TEMPLATES:
                    if re.match(imt, movie_file, flags=re.I):
                        ignore_file = True
                        break

                if ignore_file:
                    continue

                movie_path = list(filter(None, movie_name.split("/")))

                # clean paths a bit
                movie_path = list(
                    map(
                        lambda p: re.sub(r"\[.*\]$", "", p).strip(),
                        movie_path,
                    )
                )

                movies_list[movie_key] = '/'.join(movie_path) + f"#{movie['title']} ({movie.get('year')})"

    rkey_movies = f"pr:movies:{plex_server['node']}"
    if r.exists(rkey_movies):
        existing_movies_list = r.hgetall(rkey_movies)
        movies_list.update(existing_movies_list)

    r.hmset(rkey_movies, movies_list)
    r.expire(rkey_movies, 10 * 60)

    rq_queue.enqueue_in(datetime.timedelta(seconds=5),
        'rq_tasks.process_movies', retry=rq.Retry(max=3, interval=[10, 30, 60]),
         plex_server=plex_server)

def process_movies(media_container: dict = None, plex_server: dict = None) -> None:
    movies = {}
    rkey_movies = f"pr:movies:{plex_server['node']}"
    movies_list = r.hgetall(rkey_movies)
    base_path = _get_common_path(list(movies_list.values()))

    for key in r.scan_iter(f"/movies/{plex_server['node']}/*"):
        r.delete(key)

    for key in r.sscan_iter(f"/movies/{plex_server['node']}/*"):
        r.delete(key)

    for movie_key, movie_name in movies_list.items():
        # print(movie_name, base_path)
        movie_base_placeholder = movie_name.split("#")[-1]

        movie_name = movie_name.split("#")[0]
        movie_name = movie_name.replace(base_path, "").strip("/")
        movie_path = list(filter(None, movie_name.split("/")))
        movie_file = movie_path[-1]

        # add parent folder for root files
        if len(movie_path) == 1:
            movie_path = [movie_base_placeholder] + movie_path

        node = movies
        for idx, level in enumerate(movie_path):

            if idx < len(movie_path) - 1:
                node = node.setdefault(level, dict())
            else:
                d_files = DynamicAccessNestedDict(movies).getval(
                    movie_path[:-1]
                )

                if d_files:
                    d_files.update({movie_file: movie_key})
                else:
                    d_files = {movie_file: movie_key}

                DynamicAccessNestedDict(movies).setval(movie_path[:-1], d_files)

    _set_dir_structure({"movies": {plex_server["node"]: movies}})
    # return movies

def get_shows(media_container: dict = None, plex_server: dict = None) -> None:
    for show in media_container["Metadata"]:
        rq_queue.enqueue_in(datetime.timedelta(seconds=random.randint(5,120)),
            'rq_tasks.get_seasons', retry=rq.Retry(max=3, interval=[10, 30, 60]),
            show=show, plex_server=plex_server)

def get_seasons(show: dict = None, plex_server: dict = None):
    query_params = {
        "X-Plex-Token": plex_server["token"],
        "X-Plex-Container-Start": 0,
        "X-Plex-Container-Size": 100,  # no more than 100 seasons
        "excludeAllLeaves": 1,
        "includeUserState": 0,
    }

    seasons = requests.get(
        url=f"https://{plex_server['uri']}{show['key']}?{urlencode(query_params)}",
        timeout=10,
        headers=HEADERS,
    )

    for season in seasons.json()["MediaContainer"]["Metadata"]:
        rq_queue.enqueue_in(
            datetime.timedelta(seconds=random.randint(5,300)),
            'rq_tasks.get_episodes', retry=rq.Retry(max=3, interval=[10, 30, 60]),
            season=season, plex_server=plex_server)

def get_episodes(season: dict = None, plex_server: dict = None, offset: int = 0) -> None:
    episode_list = {}

    query_params = {
        "X-Plex-Token": plex_server["token"],
        "X-Plex-Container-Start": offset,
        "X-Plex-Container-Size": 100,
        "excludeAllLeaves": 1,
        "includeUserState": 0,
    }

    episodes = requests.get(
        url=f"https://{plex_server['uri']}{season['key']}?{urlencode(query_params)}",
        timeout=10,
        headers=HEADERS,
    )

    media_container = episodes.json()["MediaContainer"]
    for episode in media_container["Metadata"]:
        for media in episode["Media"]:
            if media.get("videoResolution") in IGNORE_RESOLUTIONS:
                continue

            for part in media["Part"]:
                episode_key = part.get("key")
                episode_name = part.get("file")

                if not episode_key or not episode_name:
                    continue

                if part.get("container") in IGNORE_EXTENSIONS:
                    continue

                if part["size"] / 1000000 < 50:
                    continue

                if "anime" in episode_name.lower():
                    continue

                episode_path = list(filter(None, episode_name.split("/")))
                episode_list[episode_key] = '/'.join(episode_path)

    rkey_shows = f"pr:shows:{plex_server['node']}"
    if r.exists(rkey_shows):
        existing_episodes_list = r.hgetall(rkey_shows)
        episode_list.update(existing_episodes_list)

    r.hmset(rkey_shows, episode_list)
    r.expire(rkey_shows, 10 * 60)

    if (
        media_container["size"] + media_container["offset"]
        < media_container["totalSize"]
    ):
        offset += 100
        rq_queue.enqueue_in(
                datetime.timedelta(seconds=random.randint(5,300)),
                'rq_tasks.get_episodes', retry=rq.Retry(max=3, interval=[10, 30, 60]),
                season=season, plex_server=plex_server, offset = offset)

    rq_queue.enqueue_in(datetime.timedelta(seconds=random.randint(5,300)),
        'rq_tasks.process_episodes', retry=rq.Retry(max=3, interval=[10, 30, 60]),
         plex_server=plex_server)

def process_episodes(plex_server: dict = None) -> None:
    shows = {}
    rkey_shows = f"pr:shows:{plex_server['node']}"
    episode_list = r.hgetall(rkey_shows)
    base_path = _get_common_path(list(episode_list.values()))

    for key in r.scan_iter(f"/shows/{plex_server['node']}/*"):
        r.delete(key)

    for key in r.sscan_iter(f"/shows/{plex_server['node']}/*"):
        r.delete(key)

    for episode_key, episode_name in episode_list.items():
        episode_name = episode_name.replace(base_path, "").strip("/")
        episode_path = list(filter(None, episode_name.split("/")))
        episode_file = episode_path[-1]

        # no root file or 1st ones
        if len(episode_path) == 1:
            continue

        node = shows
        for idx, level in enumerate(episode_path):
            if idx < len(episode_path) - 1:
                node = node.setdefault(level, dict())
            else:
                d_files = DynamicAccessNestedDict(shows).getval(episode_path[:-1])

                if d_files:
                    d_files.update({episode_file: episode_key})
                else:
                    d_files = {episode_file: episode_key}

                DynamicAccessNestedDict(shows).setval(episode_path[:-1], d_files)

    _set_dir_structure({"shows": {plex_server["node"]: shows}})