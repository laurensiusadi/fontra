import * as html from "@fontra/core/html-utils.js";
import Panel from "./panel.js";

// The fontra.mcp.live server (one per agent session: Claude Code, Codex, Cursor, …) serves its status API on the first free port from 8780.
const MCP_PORTS = [8780, 8781, 8782, 8783, 8784, 8785];

export default class McpPanel extends Panel {
  identifier = "mcp";
  iconPath = "/tabler-icons/antenna-bars-5.svg";

  static styles = `
    .mcp {
      --agent-color: rgb(217, 119, 87);
      --you-color: rgb(84, 130, 255);
      font-family: fontra-ui-regular, sans-serif;
      font-size: 18px;
      line-height: 1.3;
      overflow: hidden auto;
    }
    .mcp-body {
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 10px;
      min-width: 0;
    }
    .mcp-card {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 10px;
      border-radius: 8px;
      background-color: var(--text-input-background-color);
      min-width: 0;
    }
    .mcp-row {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .mcp-left {
      flex: 1;
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .mcp-meta {
      font-size: 14px;
      opacity: 0.7;
    }
    .mcp-clip {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .mcp-dot {
      flex: none;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background-color: rgb(140, 140, 140);
    }
    .mcp-dot.on { background-color: rgb(46, 160, 67); }
    .mcp-dot.warn { background-color: rgb(210, 153, 34); }
    .mcp-dot.off { background-color: transparent; box-shadow: inset 0 0 0 1.5px rgb(140, 140, 140); }
    .mcp button {
      font-family: inherit;
      font-size: 14px;
      color: inherit;
      background-color: transparent;
      border: 0.5px solid rgba(128, 128, 128, 0.5);
      border-radius: 6px;
      cursor: pointer;
    }
    .mcp button:hover { background-color: rgba(128, 128, 128, 0.15); }
    .mcp-wide {
      width: 100%;
      padding: 6px 8px;
    }
    .mcp-icon {
      flex: none;
      width: 32px;
      height: 32px;
      padding: 0;
      display: grid;
      place-items: center;
    }
    .mcp-icon svg { width: 18px; height: 18px; }
    .mcp-key { display: flex; flex-wrap: wrap; gap: 4px 12px; }
    .mcp-key span::before {
      content: "";
      display: inline-block;
      width: 10px;
      height: 10px;
      margin-right: 5px;
      border-radius: 2px;
      background-color: var(--c);
    }
    .mcp-log {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .mcp-item {
      padding-left: 8px;
      border-left: 3px solid var(--c);
      min-width: 0;
    }
    .mcp-item-title { overflow-wrap: anywhere; }
    .mcp img {
      width: 100%;
      border-radius: 6px;
      background-color: white;
    }
  `;

  constructor(editorController) {
    super(editorController);
    this.project = new URLSearchParams(location.search).get("project");
    this.ui = null;
    setInterval(() => {
      if (this.checkVisibility?.()) {
        this.update();
      }
    }, 2000);
  }

  getContentElement() {
    return html.div({ class: "panel mcp" }, [html.div({ class: "mcp-body" })]);
  }

  async toggle(on) {
    if (on) {
      this.update();
    }
  }

  async findMcp() {
    // ponytail: poll the known server; rescan all ports only when it fails or every 30 s, to keep refused probes out of the console
    this.ticks = (this.ticks || 0) + 1;
    if (this.ui && this.ticks % 15) {
      const status = await getStatus(this.ui);
      if (status) {
        return { ui: this.ui, status };
      }
    }
    const found = [];
    await Promise.all(
      MCP_PORTS.filter((port) => String(port) !== location.port).map(async (port) => {
        const ui = `http://127.0.0.1:${port}`;
        const status = await getStatus(ui);
        if (status) {
          found.push({ ui, port, status });
        }
      })
    );
    found.sort((a, b) => a.port - b.port);
    return found.find((m) => this.isThisFontra(m.status)) || found[0];
  }

  isThisFontra(status) {
    return (
      new URL(status.fontra).port === location.port && status.project === this.project
    );
  }

  async post(path, body) {
    await fetch(this.ui + path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    this.update();
  }

  async update() {
    const mcp = await this.findMcp();
    this.ui = mcp?.ui;
    const body = this.contentElement.querySelector(".mcp-body");
    body.replaceChildren(...(mcp ? this.render(mcp.status) : this.renderNone()));
  }

  renderNone() {
    return [
      card([
        row([dot(""), html.span({ textContent: "No agent" })]),
        meta(
          "Register fontra-live as an MCP server in your agent (Claude Code, Codex, Cursor, …); it answers on 127.0.0.1:8780."
        ),
      ]),
    ];
  }

  render(s) {
    const agent = s.client || "Agent";
    const here = this.isThisFontra(s);
    const [kind, label, note] = !s.link
      ? ["off", "Off", `${agent}'s tools refuse until you turn it on.`]
      : !here
        ? ["warn", "Other Fontra", otherFontra(s, this.project)]
        : s.connected
          ? ["on", "Connected", `${agent} is connected to this font.`]
          : ["", "Idle", `${agent} connects on its next tool call.`];

    const power = html.button({
      class: "mcp-icon",
      title: s.link ? "Turn off" : "Turn on",
      ariaLabel: s.link ? "Turn off" : "Turn on",
      onclick: () => this.post("/api/link", { on: !s.link }),
    });
    power.innerHTML = POWER_ICON;

    const linkCard = card([
      row([dot(kind), html.span({ class: "mcp-clip", textContent: label })], power),
      meta(note),
      ...(kind === "warn"
        ? [
            html.button({
              class: "mcp-wide",
              textContent: "Use this Fontra",
              onclick: () =>
                this.post("/api/target", {
                  url: location.origin,
                  project: this.project,
                }),
            }),
          ]
        : []),
    ]);

    const items = s.log.slice(0, 12).map((e) => {
      const who = e.source === "Fontra" ? "--you-color" : "--agent-color";
      const detail = [e.t, e.summary].filter(Boolean).join(" · ");
      return html.div({ class: "mcp-item", style: `--c: var(${who})` }, [
        html.div({ class: "mcp-item-title", textContent: e.title }),
        meta(detail, true),
      ]);
    });
    const activity = card([
      row(
        [html.span({ textContent: "Activity" })],
        html.span({ class: "mcp-meta", textContent: s.log.length })
      ),
      html.div({ class: "mcp-key mcp-meta" }, [
        html.span({ style: "--c: var(--agent-color)", textContent: agent }),
        html.span({ style: "--c: var(--you-color)", textContent: "You" }),
      ]),
      items.length ? html.div({ class: "mcp-log" }, items) : meta("Nothing yet."),
    ]);

    const cards = [linkCard, activity];
    if (s.image) {
      cards.push(
        card([
          html.div({ textContent: "Last image" }),
          meta(s.image, true),
          html.createDomElement("img", {
            src: `${this.ui}/img/${encodeURIComponent(s.image)}?t=${s.image_mtime}`,
            alt: s.image,
          }),
        ])
      );
    }
    return cards;
  }
}

// Tabler "power" icon (MIT).
const POWER_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 6a7.75 7.75 0 1 0 10 0"/><path d="M12 4l0 8"/></svg>`;

function otherFontra(s, project) {
  const where = new URL(s.fontra).host;
  return s.project === project
    ? `Working in ${where}`
    : `Working on ${s.project} in ${where}`;
}

async function getStatus(ui) {
  try {
    const r = await fetch(ui + "/api/status", { signal: AbortSignal.timeout(800) });
    return r.ok ? await r.json() : null;
  } catch {
    return null; // nothing there, or not an MCP status server
  }
}

function card(children) {
  return html.div({ class: "mcp-card" }, children);
}

// Name on the left (shrinks and clips), an optional count or action on the right.
function row(left, right) {
  return html.div({ class: "mcp-row" }, [
    html.div({ class: "mcp-left" }, left),
    ...(right ? [right] : []),
  ]);
}

function dot(kind) {
  return html.span({ class: `mcp-dot ${kind}` });
}

// clip: one line with an ellipsis, full text on hover.
function meta(text, clip = false) {
  return html.div({
    class: `mcp-meta ${clip ? "mcp-clip" : ""}`,
    textContent: text,
    title: clip ? text : "",
  });
}

customElements.define("panel-mcp", McpPanel);
