"""Live view of a training run. Reads the log the trainer is already writing,
so it attaches to a run in progress. python scripts/watch_training.py [log]"""

import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOG = Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/gat_selffix.log")
ROW = re.compile(r"epoch\s+(\d+) val illicit F1 ([\d.]+)\s+AUC-PR ([\d.]+)\s+"
                 r"precision ([\d.]+)\s+recall ([\d.]+)")


def parse():
    text = LOG.read_text(errors="ignore")
    rows = [tuple(map(float, m.groups())) for m in ROW.finditer(text)]
    done = "GAT test" in text
    header = next((l for l in text.splitlines() if "txns," in l), "")
    return rows, done, header


def chart(rows, key, colour, label):
    if not rows:
        return f'<text x="20" y="40" fill="#888">waiting for epoch 1…</text>'
    top = max(max(r[key] for r in rows), 0.05) * 1.15
    pts = " ".join(f"{60 + (r[0] - 1) * 620 / max(len(rows), 8):.0f},"
                   f"{240 - r[key] / top * 200:.0f}" for r in rows)
    best = max(rows, key=lambda r: r[key])
    dots = "".join(f'<circle cx="{60 + (r[0]-1)*620/max(len(rows),8):.0f}" '
                   f'cy="{240 - r[key]/top*200:.0f}" r="3" fill="{colour}"/>' for r in rows)
    return f"""<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="2"/>{dots}
      <text x="60" y="20" fill="{colour}" font-size="13">{label} — latest {rows[-1][key]:.4f},
      best {best[key]:.4f} (epoch {best[0]:.0f})</text>
      <text x="8" y="45" fill="#888" font-size="11">{top:.2f}</text>
      <text x="8" y="243" fill="#888" font-size="11">0</text>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        rows, done, header = parse()
        status = ("finished" if done else
                  f"running — epoch {int(rows[-1][0])}" if rows else "starting up")
        body = f"""<!doctype html><meta http-equiv="refresh" content="20">
<title>AML GAT training</title>
<style>body{{background:#111;color:#ddd;font:14px ui-monospace,monospace;padding:24px}}
h1{{font-size:16px;font-weight:600}} .s{{color:{'#4ade80' if done else '#fbbf24'}}}
svg{{background:#181818;border-radius:8px;margin:12px 0}} td{{padding:2px 14px 2px 0}}</style>
<h1>AML GAT training <span class="s">[{status}]</span></h1>
<div style="color:#888">{header}<br>{LOG} · refreshes every 20s</div>
<svg width="700" height="260">{chart(rows, 1, "#60a5fa", "validation illicit F1")}</svg>
<svg width="700" height="260">{chart(rows, 2, "#f472b6", "validation AUC-PR")}</svg>
<table><tr style="color:#888"><td>epoch<td>F1<td>AUC-PR<td>precision<td>recall</tr>
{"".join(f"<tr><td>{int(r[0])}<td>{r[1]:.4f}<td>{r[2]:.4f}<td>{r[3]:.4f}<td>{r[4]:.4f}</tr>"
         for r in reversed(rows))}</table>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


print(f"watching {LOG} -> http://localhost:8765")
HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
