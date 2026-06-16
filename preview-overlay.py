#!/usr/bin/env python3
"""Focus-safe, translucent on-screen preview for push-to-talk dictation.

A GTK POPUP window (override-redirect, so it never takes keyboard focus) with an
RGBA visual: the background is painted semi-transparent via cairo while the text
stays fully opaque and readable. Forced onto XWayland so GNOME composites the
per-pixel alpha. Reads text lines on stdin and shows the latest; exits on EOF.
"""
import os
import sys
import threading

os.environ.setdefault("GDK_BACKEND", "x11")   # XWayland: override-redirect + RGBA work under GNOME

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
import cairo

BG_ALPHA  = 0.55      # background opacity (0 = invisible, 1 = solid); text stays opaque
RADIUS    = 16
MAX_CHARS = 360       # tail shown; keeps the box a sane height (a few lines)

win = Gtk.Window(type=Gtk.WindowType.POPUP)    # POPUP = override-redirect: no focus steal
win.set_accept_focus(False)
win.set_keep_above(True)
win.set_app_paintable(True)

screen = win.get_screen()
visual = screen.get_rgba_visual()
if visual is not None:
    win.set_visual(visual)

sw = screen.get_width()
W = min(1200, sw - 80)
win.set_size_request(W, -1)
win.move((sw - W) // 2, 56)

label = Gtk.Label()
label.set_line_wrap(True)
label.set_xalign(0.0)
label.set_yalign(0.0)
label.set_property("margin", 20)
label.set_size_request(W - 40, -1)     # fix wrap width so height is computed against it
win.add(label)


def show(text):
    if not text:
        text = "listening…"
    elif len(text) > MAX_CHARS:
        text = "…" + text[-MAX_CHARS:]
    safe = GLib.markup_escape_text("🎙  " + text)
    label.set_markup(f'<span foreground="#ffffff" font_desc="Sans Bold 18">{safe}</span>')
    _, nat_h = label.get_preferred_height_for_width(W - 40)   # grow the box to fit wrapped lines
    win.resize(W, nat_h + 40)


def on_draw(widget, cr):
    w = widget.get_allocated_width()
    h = widget.get_allocated_height()
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)             # clear to fully transparent
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    r = RADIUS
    cr.new_sub_path()
    cr.arc(w - r, r,     r, -1.5708, 0)
    cr.arc(w - r, h - r, r, 0,        1.5708)
    cr.arc(r,     h - r, r, 1.5708,   3.1416)
    cr.arc(r,     r,     r, 3.1416,   4.7124)
    cr.close_path()
    cr.set_source_rgba(0, 0, 0, BG_ALPHA)      # translucent background
    cr.fill()
    return False


win.connect("draw", on_draw)


def reader():
    for line in sys.stdin:
        text = line.rstrip("\n")
        GLib.idle_add(show, text)
    GLib.idle_add(Gtk.main_quit)


show("")
win.show_all()
threading.Thread(target=reader, daemon=True).start()
Gtk.main()
