import argparse
import asyncio
import logging
from pathlib import Path

import aiolimiter
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from streamrip.client import DeezerClient
from streamrip.config import Config
from streamrip.db import Database, Downloads, Failed
from streamrip.exceptions import NonStreamableError
from streamrip.media import PendingAlbum, PendingTrack

import spotirip.arls

logger = logging.getLogger(__name__)
auth_manager = SpotifyClientCredentials()
sp = spotipy.Spotify(auth_manager=auth_manager)


class AlbumFilter:
    def __init__(self, whole: bool, tracks: set[str]):
        self.whole = whole
        self.tracks = tracks

    def __repr__(self) -> str:
        return f"Whole: {self.whole} Tracks: {self.tracks}"


def get_spotify_playlist(playlist_id: str) -> list[str]:
    logger.getChild("get_spotify_playlist").debug(f"playlist_id: {playlist_id}")

    spotify_playlist = sp.playlist(playlist_id)
    whole_playlist = spotify_playlist["tracks"]["items"]
    while spotify_playlist["tracks"]["next"]:
        spotify_playlist["tracks"] = sp.next(spotify_playlist["tracks"])
        whole_playlist.extend(spotify_playlist["tracks"]["items"])

    track_infos: list[str] = []
    for track in whole_playlist:
        if track["track"]["external_ids"]["isrc"] is None:
            continue
        track_infos.append(f'isrc:{track["track"]["external_ids"]["isrc"]}')

    return track_infos


async def get_track_id(c: DeezerClient, track_info: str, limiter: aiolimiter.AsyncLimiter) -> tuple[str, str]:
    async with limiter:
        track_data = await c.get_track(track_info)
        return track_data["id"], track_data["album"]["id"]


def get_album_id(c: DeezerClient, album_uri: str) -> str:
    album = sp.album(album_uri)
    upc = album["external_ids"]["upc"]
    return c.client.api.get_album_by_UPC(upc)["id"]


async def download_track(ptr: PendingTrack, limiter: aiolimiter.AsyncLimiter) -> None:
    clogger = logger.getChild("download_track")
    clogger.debug("entering")

    async with limiter:
        tr = await ptr.resolve()
        logger.debug("Resolved track!")
        if tr is None:
            return

        logger.info(f"Downloading track {tr.meta.title}")

        await tr.preprocess()
        await tr.download()
        await tr.postprocess()

        return


async def download_album(
    c: DeezerClient,
    db: Database,
    config: Config,
    album_id: str,
    album_filter: AlbumFilter,
    limiter: aiolimiter.AsyncLimiter,
) -> None:
    dlogger = logger.getChild("download_album")
    dlogger.debug(f"album_id: {album_id}")

    async with limiter:
        pal = PendingAlbum(album_id, c, config, db)
        ral = await pal.resolve()

    logger.debug("resolved album")
    ptrs = ral.tracks
    if not album_filter.whole:
        ptrs = [t for t in ral.tracks if t.id in album_filter.tracks]

    await asyncio.gather(*[download_track(ptr, limiter) for ptr in ptrs])


def read_ids_from_file(file_path: Path) -> list[str]:
    with Path.open(file_path) as fp:
        return [s.removesuffix("\n") for s in fp.readlines()]


async def main(config_path: Path, debug: bool, playlists: Path, albums: Path):
    arls = spotirip.arls.retrieve_arls()

    config = Config(config_path)
    config.session.deezer.arl = await spotirip.arls.find_working_arl(arls)
    c = DeezerClient(config)
    await c.login()

    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.addHandler(logging.StreamHandler())
    logger.info("Logged into Deezer!")

    download_dir = config.session.downloads.folder
    db_path = Path(download_dir)
    if not db_path.exists():
        db_path.mkdir()
    db = Database(Downloads(db_path / "downloads.sl3"), Failed(db_path / "failed.sl3"))

    sp_playlists = read_ids_from_file(playlists)
    sp_albums = read_ids_from_file(albums)

    playlist_tracks: list[str] = []
    for sp_pl in sp_playlists:
        playlist_tracks.extend(get_spotify_playlist(sp_pl))

    album_ids = [get_album_id(c, uri) for uri in sp_albums]

    limiter = aiolimiter.AsyncLimiter(20, 1)
    albums: dict[str, AlbumFilter] = {}

    try:
        pl_tr_infos = await asyncio.gather(*[get_track_id(c, track_info, limiter) for track_info in playlist_tracks])
    except NonStreamableError as e:
        logger.warning(f"Failed to find track on Deezer: {e}")

    for track_id, album_id in pl_tr_infos:
        if not albums.get(album_id):
            albums[album_id] = AlbumFilter(False, {track_id})
        else:
            albums[album_id].tracks.add(track_id)

    for album_id in album_ids:
        if not albums.get(album_id):
            albums[album_id] = AlbumFilter(True, set())
        else:
            albums[album_id].whole = True

    await asyncio.gather(
        *[download_album(c, db, config, album, album_filter, limiter) for album, album_filter in albums.items()]
    )

    await c.session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path, help="Path to config file")
    parser.add_argument("--debug", action=argparse.BooleanOptionalAction, help="Enable debug logs")
    parser.add_argument(
        "playlists",
        type=Path,
        help="List of Spotify playlists to download",
    )
    parser.add_argument(
        "albums",
        type=Path,
        help="List of Spotify albums to download",
    )

    args = parser.parse_args()
    asyncio.run(main(args.config, args.debug, args.playlists, args.albums))
