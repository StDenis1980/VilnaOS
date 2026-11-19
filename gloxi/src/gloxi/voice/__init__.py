# SPDX-License-Identifier: GPL-3.0-or-later
"""Gloxi's voice, on this computer only (ADR 0015).

- ``models``: what can be downloaded (speech recognition, voices), pinned by SHA-256.
- ``store``: downloading after the person agrees, and unpacking safely.
- ``language``: which of the ten languages a text is in, and the interface language.
- ``phonemes``: text to the sounds a voice is trained on (eSpeak NG, or letters).
- ``speech``: what is read aloud (no passwords, no Markdown, no addresses).
- ``stt``: speech to text with Parakeet TDT 0.6B v3 and the Silero VAD.
- ``tts``: text to speech with the voices of ``models`` or eSpeak NG.
- ``audio``: the microphone and the speakers through GStreamer (PipeWire).

Rules that hold everywhere here: sound never leaves the computer and is never
written to disk; the microphone is open only between a press and the end of
the phrase; Gloxi does not listen while it speaks.

The sherpa-onnx engine is imported only when voice is used, so Gloxi without
it (and its tests) works as before.
"""
