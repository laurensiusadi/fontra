# Fontra live loop

`fontra.mcp.live` is a stdio MCP server that joins a running `fontra filesystem` project as a second websocket client. Any MCP agent (Claude Code, Codex, Cursor, …) reads, writes, renders and measures the same glyph the user has open, with no export step.

Register it in the agent as a stdio server. The command is the Fontra venv's python, and the arguments are `-m fontra.mcp.live <abs>/probe/edit OvernightSans-Regular.ufo`. For Claude Code:

```bash
claude mcp add fontra-live -- <fontra-venv>/bin/python -m fontra.mcp.live <abs>/probe/edit OvernightSans-Regular.ufo
```

The server reads the agent's name from the MCP handshake (`clientInfo`) and shows it on the status page and the editor panel.

Given a folder, the server starts `fontra filesystem` on it at port 8769 (`FONTRA_PORT`) on the first tool call if nothing answers there, detached so it outlives the MCP server; the log is `/tmp/fontra-live/fontra.log`. Given a URL instead, it only connects. The user's tab is `http://localhost:8769/editor.html?project=OvernightSans-Regular.ufo`.

## Tools

| Tool | What it returns |
|---|---|
| get_glyph | Points `[x, y, type]`, contours, advance |
| put_glyph | Replaces the outline or nudges points; returns moved points, outline movement in units, and check issues the edit introduced |
| changes_since | The user's edits since the agent last looked: moved points, regions where the outline moved (signed, + ink added), ink change |
| check | Extra nodes, bent smooth nodes, near-smooth kinks, crooked lines and handles (units off axis), near-miss alignments and metric lines, missing extremes, curvature jumps and reversals, S-bends, tiny segments, handles past their crossing, contour direction |
| diff | The live glyph against `base` (edit-base OTF), `previous` (newest edit-history save), `snapshot`, `layer:NAME` or a font path; image with the difference amplified ×10 |
| render | Outline, nodes, handles, point numbers; optional reference font, baseline overlay, curvature comb, circled issues, zoom on a point with a 1-unit grid |
| measure | `n_anatomy` |
| status | Status page URL, Fontra up, link on |

`fontra.mcp.glyphcheck` holds the checks, diff and drawing, and runs on its own against an OTF or UFO: `python -m fontra.mcp.glyphcheck FONT GLYPH [--against FONT2] [--png OUT] [--zoom POINT:RADIUS]`.

## Status page

`http://127.0.0.1:8780` (next free port up to 8785, one per agent session running the server; `FONTRA_LIVE_UI_PORT` moves the range). It shows whether the agent is connected, the Fontra server with Start and Stop, the MCP process, recent activity from both sides, and the last image the agent drew. Turning the link off makes every tool refuse until it is back on. After Stop, the agent does not restart Fontra on its own. The MCP process itself is started and stopped by the agent (in Claude Code, `/mcp`); the page goes away with it.

## MCP panel in the editor

The editor's right sidebar has an MCP panel (signal-bars icon, `panel-mcp.js`) that reads the same status API: the link (Connected, Idle, Off, or Other Fontra when the agent works on another server or project), a power button, recent activity marked with the agent's name or You, and the last image. It fits the sidebar's 200-pixel minimum: long details end in an ellipsis with the full text on hover. "Use this Fontra" points the agent at the server and project the panel is open in (`POST /api/target`). The API answers CORS requests from localhost pages only. With several agent sessions running, the panel shows the one working on this Fontra, else the first.

## Demo result (Nov 20, scratch copy of probe/edit)

| Step | Result |
|---|---|
| Open n in Fontra in the browser pane | Edit mode, live server on port 8123 |
| `put_glyph` moves points 6–8 of n up 60 | The open tab redrew at once, no reload; the .glif on disk updated |
| User nudges the counter crest 30 down (arrow keys in the editor) | `changes_since` reported points 15–17 moved by about −30, from 3 pushed edits |
| `render` over `edit-base/OvernightSans-Regular.otf` | Overlay PNG, live red over baseline black, same x-height |
| `measure` n_anatomy | arch_top 69 → 157, notch_depth 74.5 → 134.5 after both edits |
| Cmd+Z ×4 in the tab | Undid the user's 3 nudges; the agent's edit stayed |
| `put_glyph` moves n's point 21 by 3 u (stem leans 0.34°) | Returned "line 21-22 is 3.0 u off vertical" and "points 21 and 22 almost share x, 3.0 u apart" as new issues |
| User nudges the counter up 2 u | `changes_since`: region on segment 16-19, −1.97 u, ink −0.47% |
| `diff` against the previous save, ×20 | Both edits as regions (−2.98 u on 21-22, −1.97 u on 16-19); the lean is plain in the image |
| Status page: link off, Fontra stop, start | Tools refused with the reason and the page URL; Fontra stopped and was not auto-restarted; Start brought it back and tools worked again |

## The protocol

- Transport: one websocket at `/websocket?project=<id>`; for `fontra filesystem DIR` the id is the path under DIR (`OvernightSans-Regular.ufo`). First message `{"client-uuid": …}`.
- Client → server calls: `{"client-call-id", "method-name", "arguments"}`, answered with `return-value` or `exception`. Methods on `FontHandler`: `getGlyph`, `getGlyphMap`, `getSources`, `getAxes`, `getFontInfo`, `subscribeChanges(pattern, wantLive)`, `editIncremental(change)`, `editFinal(change, rollback, label, broadcast)` and more.
- Server → client calls: `externalChange(change, isLive)` and `reloadData(pattern)` (the backend file watcher saw an outside write), answered with `server-call-id`.
- A change is `{"p": path, "f": func, "a": args, "c": children}`; funcs include `=`, `+`, `-`, `=xy`, `insertPoint`, `deleteContour`. `put_glyph` sends `=` on `["glyphs", n, "layers", layer, "glyph"]` for `path` (and `xAdvance`).
- The server keeps the glyph in memory with every client's edits applied and writes it to the UFO a moment later, so `getGlyph` is always the current state.

## Supported and not

| Topic | Status |
|---|---|
| Live push to the open tab | Works: `editFinal(…, broadcast=True)` reaches every other subscribed client, and the editor redraws |
| User's edits to the agent | Works: subscribe to `{"glyphs": null}`; each finished edit arrives as `externalChange` |
| In-progress drags | Not seen: the server only subscribes to final changes; `wantLiveChanges=True` would stream them |
| Undo | One-sided: the server ignores `rollbackChange` (a TODO in `editFinal`); the tab's undo stack holds only its own edits, so Cmd+Z never reverts the agent, and undoing a user edit after the agent changed the same points would overwrite the agent's change |
| Multiple clients | Any number; no locking or conflict check (another TODO), last write wins |
| Variable sources | Each source is a layer; `layer` picks one, default the first source. The edit UFOs are one master per project; a designspace project would expose all masters in one glyph |
| Open contours, components, anchors | Not handled by `put_glyph` (closed contours only; components untouched) |
| Undo for the agent's edits | Not built: `put_glyph` returns the old outline in its diff; re-put it to revert |

<details><summary>Details</summary>

- `changes_since` diffs against the last outline the agent read or wrote for that glyph, not against `edit-base`; the first call only takes the baseline.
- Arrow-key nudges in Fontra round coordinates to integers, so a 30-unit nudge shows as −30.07 on a point that started at 435.07.
- The MCP layer is hand-written JSON-RPC (initialize, tools/list, tools/call, ping) because the Fontra venv has no `mcp` package.
- `measure` names: `n_anatomy` (port of the scratch nmeasure.py, now in font units instead of x-height 508).

</details>
