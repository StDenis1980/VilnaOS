# SPDX-License-Identifier: GPL-3.0-or-later
"""The microphone and the speakers, through GStreamer and PipeWire (ADR 0015, section 4).

In the Flatpak both go through ``--socket=pulseaudio`` (PipeWire's Pulse
server); GNOME Shell shows its microphone indicator while ``Microphone``
runs. The microphone gives 16 kHz mono float samples, as the speech model
wants, to a callback on GStreamer's thread; nothing is written anywhere.
"""

from __future__ import annotations

import sys
import threading
from array import array
from collections.abc import Callable

SOURCES = ("pulsesrc", "autoaudiosrc")
SINKS = ("pulsesink", "autoaudiosink")


class AudioError(Exception):
    """No sound system, or the device could not be opened; the message is for logs."""


def _gst():
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise AudioError("GStreamer is not available") from exc
    if not Gst.is_initialized():
        Gst.init(None)
    return Gst


def _first_element(Gst, names: tuple[str, ...]) -> str:
    for name in names:
        if Gst.ElementFactory.find(name) is not None:
            return name
    raise AudioError(f"none of {', '.join(names)} is installed")


def _floats(data: bytes) -> array:
    samples = array("f")
    samples.frombytes(data[: len(data) - len(data) % 4])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


class Microphone:
    """Sound from the default input until ``stop``; ``on_samples`` gets float arrays."""

    def __init__(
        self,
        on_samples: Callable[[array], None],
        *,
        on_error: Callable[[str], None] = lambda why: None,
        rate: int = 16_000,
        source: str | None = None,
    ) -> None:
        self.on_samples = on_samples
        self.on_error = on_error
        self.rate = rate
        self.source = source
        self._pipeline = None
        self._lock = threading.Lock()

    def start(self) -> None:
        Gst = _gst()
        source = self.source or _first_element(Gst, SOURCES)
        description = (
            f"{source} ! audioconvert ! audioresample ! "
            f"audio/x-raw,format=F32LE,channels=1,rate={self.rate} ! "
            "appsink name=sink emit-signals=true sync=false"
        )
        try:
            pipeline = Gst.parse_launch(description)
        except Exception as exc:  # GLib.Error
            raise AudioError(f"the microphone could not be opened: {exc}") from exc
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        with self._lock:
            self._pipeline = pipeline
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise AudioError("the microphone could not be started")

    def _on_sample(self, sink) -> int:
        Gst = _gst()
        sample = sink.emit("pull-sample")
        if sample is not None:
            buffer = sample.get_buffer()
            ok, info = buffer.map(Gst.MapFlags.READ)
            if ok:
                try:
                    samples = _floats(bytes(info.data))
                finally:
                    buffer.unmap(info)
                self.on_samples(samples)
        return Gst.FlowReturn.OK

    def _on_bus_error(self, _bus, message) -> None:
        error, _debug = message.parse_error()
        self.stop()
        self.on_error(str(error))

    def stop(self) -> None:
        with self._lock:
            pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            Gst = _gst()
            pipeline.get_bus().remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)

    @property
    def running(self) -> bool:
        return self._pipeline is not None


class Player:
    """Plays float samples pushed one sentence at a time; ``stop`` cuts it at once."""

    def __init__(
        self,
        *,
        on_done: Callable[[], None] = lambda: None,
        on_error: Callable[[str], None] = lambda why: None,
        sink: str | None = None,
    ) -> None:
        self.on_done = on_done
        self.on_error = on_error
        self.sink = sink
        self._pipeline = None
        self._src = None
        self._rate = 0
        self._lock = threading.Lock()

    def push(self, samples: array, rate: int) -> None:
        """Queue sound; the first push opens the output at ``rate``."""
        Gst = _gst()
        with self._lock:
            if self._pipeline is None:
                self._open(Gst, rate)
            src = self._src
        if rate != self._rate:
            raise AudioError("one answer is read with one voice")
        data = array("f", samples)
        if sys.byteorder == "big":
            data.byteswap()
        src.emit("push-buffer", Gst.Buffer.new_wrapped(data.tobytes()))

    def _open(self, Gst, rate: int) -> None:
        sink = self.sink or _first_element(Gst, SINKS)
        description = (
            f"appsrc name=src format=time is-live=false "
            f"caps=audio/x-raw,format=F32LE,channels=1,rate={rate},layout=interleaved ! "
            f"audioconvert ! audioresample ! {sink}"
        )
        try:
            pipeline = Gst.parse_launch(description)
        except Exception as exc:  # GLib.Error
            raise AudioError(f"the speakers could not be opened: {exc}") from exc
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", lambda *_a: self._finished())
        bus.connect("message::error", self._on_bus_error)
        self._pipeline, self._src, self._rate = pipeline, pipeline.get_by_name("src"), rate
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._pipeline = self._src = None
            raise AudioError("the speakers could not be started")

    def finish(self) -> None:
        """No more sound will come: ``on_done`` runs when the last is heard."""
        with self._lock:
            src = self._src
        if src is None:
            self.on_done()
        else:
            src.emit("end-of-stream")

    def _finished(self) -> None:
        self._close()
        self.on_done()

    def _on_bus_error(self, _bus, message) -> None:
        error, _debug = message.parse_error()
        self._close()
        self.on_error(str(error))

    def stop(self) -> None:
        """Silence now (the Stop button, Esc, or the microphone being pressed)."""
        self._close()

    def _close(self) -> None:
        with self._lock:
            pipeline, self._pipeline, self._src = self._pipeline, None, None
        if pipeline is not None:
            Gst = _gst()
            pipeline.get_bus().remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)

    @property
    def playing(self) -> bool:
        return self._pipeline is not None
