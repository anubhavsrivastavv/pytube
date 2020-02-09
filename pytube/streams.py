# -*- coding: utf-8 -*-

"""
This module contains a container for stream manifest data.

A container object for the media stream (video only / audio only / video+audio
combined). This was referred to as ``Video`` in the legacy pytube version, but
has been renamed to accommodate DASH (which serves the audio and video
separately).
"""

import io
import logging
import os
import pprint
from typing import Dict, Tuple, Optional

from pytube import extract
from pytube import request
from pytube.helpers import safe_filename, target_directory
from pytube.itags import get_format_profile
from pytube.monostate import Monostate

logger = logging.getLogger(__name__)


class Stream:
    """Container for stream manifest data."""

    def __init__(self, stream: Dict, player_config_args: Dict, monostate: Monostate):
        """Construct a :class:`Stream <Stream>`.

        :param dict stream:
            The unscrambled data extracted from YouTube.
        :param dict player_config_args:
            The data object containing video media data like title and
            keywords.
        :param dict monostate:
            Dictionary of data shared across all instances of
            :class:`Stream <Stream>`.
        """
        # A dictionary shared between all instances of :class:`Stream <Stream>`
        # (Borg pattern).
        self._monostate = monostate

        self.url = stream["url"]  # signed download url
        self.itag = int(stream["itag"])  # stream format id (youtube nomenclature)

        # set type and codec info

        # 'video/webm; codecs="vp8, vorbis"' -> 'video/webm', ['vp8', 'vorbis']
        self.mime_type, self.codecs = extract.mime_type_codec(stream["type"])

        # 'video/webm' -> 'video', 'webm'
        self.type, self.subtype = self.mime_type.split("/")

        # ['vp8', 'vorbis'] -> video_codec: vp8, audio_codec: vorbis. DASH
        # streams return NoneType for audio/video depending.
        self.video_codec, self.audio_codec = self.parse_codecs()

        self._filesize: Optional[int] = None  # filesize in bytes

        # Additional information about the stream format, such as resolution,
        # frame rate, and whether the stream is live (HLS) or 3D.
        itag_profile = get_format_profile(self.itag)
        self.is_dash = itag_profile["is_dash"]
        self.abr = itag_profile["abr"]  # average bitrate (audio streams only)
        self.fps = itag_profile["fps"]  # frames per second (video streams only)
        self.resolution = itag_profile["resolution"]  # resolution (e.g.: "480p")
        self.is_3d = itag_profile["is_3d"]
        self.is_hdr = itag_profile["is_hdr"]
        self.is_live = itag_profile["is_live"]

        # The player configuration, contains info like the video title.
        self.player_config_args = player_config_args

    @property
    def is_adaptive(self) -> bool:
        """Whether the stream is DASH.

        :rtype: bool
        """
        # if codecs has two elements (e.g.: ['vp8', 'vorbis']): 2 % 2 = 0
        # if codecs has one element (e.g.: ['vp8']) 1 % 2 = 1
        return bool(len(self.codecs) % 2)

    @property
    def is_progressive(self) -> bool:
        """Whether the stream is progressive.

        :rtype: bool
        """
        return not self.is_adaptive

    @property
    def includes_audio_track(self) -> bool:
        """Whether the stream only contains audio.

        :rtype: bool
        """
        return self.is_progressive or self.type == "audio"

    @property
    def includes_video_track(self) -> bool:
        """Whether the stream only contains video.

        :rtype: bool
        """
        return self.is_progressive or self.type == "video"

    def parse_codecs(self) -> Tuple[Optional[str], Optional[str]]:
        """Get the video/audio codecs from list of codecs.

        Parse a variable length sized list of codecs and returns a
        constant two element tuple, with the video codec as the first element
        and audio as the second. Returns None if one is not available
        (adaptive only).

        :rtype: tuple
        :returns:
            A two element tuple with audio and video codecs.

        """
        video = None
        audio = None
        if not self.is_adaptive:
            video, audio = self.codecs
        elif self.includes_video_track:
            video = self.codecs[0]
        elif self.includes_audio_track:
            audio = self.codecs[0]
        return video, audio

    @property
    def filesize(self) -> int:
        """File size of the media stream in bytes.

        :rtype: int
        :returns:
            Filesize (in bytes) of the stream.
        """
        if self._filesize is None:
            headers = request.headers(self.url)
            self._filesize = int(headers["content-length"])
        return self._filesize

    @property
    def title(self) -> str:
        """Get title of video

        :rtype: str
        :returns:
            Youtube video title
        """
        return (
            self.player_config_args.get("title")
            or (
                self.player_config_args.get("player_response", {})
                .get("videoDetails", {})
                .get("title")
            )
            or "Unknown YouTube Video Title"
        )

    @property
    def default_filename(self) -> str:
        """Generate filename based on the video title.

        :rtype: str
        :returns:
            An os file system compatible filename.
        """
        filename = safe_filename(self.title)
        return f"{filename}.{self.subtype}"

    def download(
        self,
        output_path: Optional[str] = None,
        filename: Optional[str] = None,
        filename_prefix: Optional[str] = None,
        skip_existing: bool = True,
    ) -> str:
        """Write the media stream to disk.

        :param output_path:
            (optional) Output path for writing media file. If one is not
            specified, defaults to the current working directory.
        :type output_path: str or None
        :param filename:
            (optional) Output filename (stem only) for writing media file.
            If one is not specified, the default filename is used.
        :type filename: str or None
        :param filename_prefix:
            (optional) A string that will be prepended to the filename.
            For example a number in a playlist or the name of a series.
            If one is not specified, nothing will be prepended
            This is separate from filename so you can use the default
            filename but still add a prefix.
        :type filename_prefix: str or None
        :param skip_existing:
            (optional) skip existing files, defaults to True
        :type skip_existing: bool
        :returns:
            Path to the saved video
        :rtype: str

        """
        if filename:
            filename = f"{safe_filename(filename)}.{self.subtype}"
        else:
            filename = self.default_filename

        if filename_prefix:
            filename = f"{safe_filename(filename_prefix)}{filename}"

        file_path = os.path.join(target_directory(output_path), filename)

        if (
            skip_existing
            and os.path.isfile(file_path)
            and os.path.getsize(file_path) == self.filesize
        ):
            # likely the same file, so skip it
            logger.debug("file %s already exists, skipping", file_path)
            return file_path

        bytes_remaining = self.filesize
        logger.debug(
            "downloading (%s total bytes) file to %s", self.filesize, file_path,
        )

        with open(file_path, "wb") as fh:
            for chunk in request.stream(self.url):
                # reduce the (bytes) remainder by the length of the chunk.
                bytes_remaining -= len(chunk)
                # send to the on_progress callback.
                self.on_progress(chunk, fh, bytes_remaining)
        self.on_complete(fh)
        return file_path

    def stream_to_buffer(self) -> io.BytesIO:
        """Write the media stream to buffer

        :rtype: io.BytesIO buffer
        """
        buffer = io.BytesIO()
        bytes_remaining = self.filesize
        logger.debug(
            "downloading (%s total bytes) file to BytesIO buffer", self.filesize,
        )

        for chunk in request.stream(self.url):
            # reduce the (bytes) remainder by the length of the chunk.
            bytes_remaining -= len(chunk)
            # send to the on_progress callback.
            self.on_progress(chunk, buffer, bytes_remaining)
        self.on_complete(buffer)
        return buffer

    def on_progress(self, chunk, file_handler, bytes_remaining):
        """On progress callback function.

        This function writes the binary data to the file, then checks if an
        additional callback is defined in the monostate. This is exposed to
        allow things like displaying a progress bar.

        :param bytes chunk:
            Segment of media file binary data, not yet written to disk.
        :param file_handler:
            The file handle where the media is being written to.
        :type file_handler:
            :py:class:`io.BufferedWriter`
        :param int bytes_remaining:
            The delta between the total file size in bytes and amount already
            downloaded.

        :rtype: None

        """
        file_handler.write(chunk)
        logger.debug(
            "download progress\n%s",
            pprint.pformat(
                {"chunk_size": len(chunk), "bytes_remaining": bytes_remaining,},
                indent=2,
            ),
        )
        on_progress = self._monostate.on_progress
        if on_progress:
            logger.debug("calling on_progress callback %s", on_progress)
            on_progress(self, chunk, file_handler, bytes_remaining)

    def on_complete(self, file_handle):
        """On download complete handler function.

        :param file_handle:
            The file handle where the media is being written to.
        :type file_handle:
            :py:class:`io.BufferedWriter`

        :rtype: None

        """
        logger.debug("download finished")
        on_complete = self._monostate.on_complete
        if on_complete:
            logger.debug("calling on_complete callback %s", on_complete)
            on_complete(self, file_handle)

    def __repr__(self) -> str:
        """Printable object representation.

        :rtype: str
        :returns:
            A string representation of a :class:`Stream <Stream>` object.
        """
        parts = ['itag="{s.itag}"', 'mime_type="{s.mime_type}"']
        if self.includes_video_track:
            parts.extend(['res="{s.resolution}"', 'fps="{s.fps}fps"'])
            if not self.is_adaptive:
                parts.extend(
                    ['vcodec="{s.video_codec}"', 'acodec="{s.audio_codec}"',]
                )
            else:
                parts.extend(['vcodec="{s.video_codec}"'])
        else:
            parts.extend(['abr="{s.abr}"', 'acodec="{s.audio_codec}"'])
        parts.extend(['progressive="{s.is_progressive}"', 'type="{s.type}"'])
        return f"<Stream: {' '.join(parts).format(s=self)}>"
