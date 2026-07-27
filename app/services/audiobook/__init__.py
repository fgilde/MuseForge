"""AudioBook Creator backend (PLAN-text-audiobook.md §3).

Pure-logic foundation: data model, persistence, text import, TTS parameter
mapping and ffmpeg mix planning.  Nothing in this package starts a job, a
subprocess or a thread — the endpoints in ``launch.py`` own that.
"""
