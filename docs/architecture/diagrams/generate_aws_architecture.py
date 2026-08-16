#!/usr/bin/env python3
"""Regenerate aws-architecture.drawio.

    python docs/architecture/diagrams/generate_aws_architecture.py

The .drawio file is a build artefact of this script: edit the script, not the
XML, or the next regeneration silently discards the change.

Every style string below was verified against jgraph/drawio dev:
  js/diagramly/sidebar/Sidebar-AWS4.js  (palette style prefixes + resIcon tokens)
  stencils/aws4.xml                     (1038 <shape name="..."> entries)

Two shape families, do not mix them:
  * SERVICE TILE  -> shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.X;  78x78
  * PLAIN RESOURCE-> shape=mxgraph.aws4.X;  no resIcon, native aspect (aspect=fixed)

All cells use parent="1" with ABSOLUTE coordinates. Confirmed correct: cell "1"
is the default layer at origin (0,0). Z-order is document order, so containers
are emitted before the shapes drawn on top of them.
"""
from pathlib import Path

#: Written next to this script, so the checkout location does not matter.
OUT = Path(__file__).resolve().parent / "aws-architecture.drawio"

# --------------------------------------------------------------------------- #
# palette (AWS 2023 category fills, verified)
# --------------------------------------------------------------------------- #
COMPUTE   = "#ED7100"
STORAGE   = "#7AA116"
APPINT    = "#E7157B"   # Application Integration AND Management & Governance
SECURITY  = "#DD344C"
NETWORK   = "#8C4FFF"
DATABASE  = "#C925D1"
GENERAL   = "#232F3D"   # plain general-resource shapes
INK       = "#232F3E"   # AWS "squid ink" - all label text
EC2ORANGE = "#D86613"   # EC2-instance-contents group chrome
SGRED     = "#DD3522"
GREY      = "#5A6C86"

RUNTIME_BADGE = INK       # numbered runtime steps
DEPLOY_BADGE  = "#3334B9" # lettered deploy steps
EXT_STROKE    = "#7D8998"

cells: list[str] = []
_boxes: dict[str, tuple[float, float, float, float]] = {}


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def add(xml: str) -> None:
    cells.append("        " + xml)


def _geo(x, y, w, h) -> str:
    return f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry" />'


def vertex(cid, x, y, w, h, label, style) -> None:
    _boxes[cid] = (x, y, w, h)
    add(f'<mxCell id="{cid}" value="{esc(label)}" style="{style}" vertex="1" '
        f'parent="1">\n          {_geo(x, y, w, h)}\n        </mxCell>')


# --------------------------------------------------------------------------- #
# style builders
# --------------------------------------------------------------------------- #
_TILE_PTS = ("points=[[0,0,0],[0.25,0,0],[0.5,0,0],[0.75,0,0],[1,0,0],[0,1,0],"
             "[0.25,1,0],[0.5,1,0],[0.75,1,0],[1,1,0],[0,0.25,0],[0,0.5,0],"
             "[0,0.75,0],[1,0.25,0],[1,0.5,0],[1,0.75,0]]")
_GRP_PTS = ("points=[[0,0],[0.25,0],[0.5,0],[0.75,0],[1,0],[1,0.25],[1,0.5],"
            "[1,0.75],[1,1],[0.75,1],[0.5,1],[0.25,1],[0,1],[0,0.75],[0,0.5],"
            "[0,0.25]]")
_GRP_PREFIX = (f"{_GRP_PTS};outlineConnect=0;gradientColor=none;html=1;"
               "whiteSpace=wrap;fontSize=13;fontStyle=1;container=0;"
               "pointerEvents=0;collapsible=0;recursiveResize=0;"
               "shape=mxgraph.aws4.group;")


def tile(cid, x, y, label, res_icon, fill, size=78, fontsize=11):
    """AWS service tile (rounded coloured square + white glyph)."""
    style = (f"sketch=0;{_TILE_PTS};outlineConnect=0;fontColor={INK};"
             f"fillColor={fill};strokeColor=#ffffff;dashed=0;"
             f"verticalLabelPosition=bottom;verticalAlign=top;align=center;"
             f"html=1;fontSize={fontsize};fontStyle=0;aspect=fixed;"
             f"shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{res_icon};")
    vertex(cid, x, y, size, size, label, style)


def plain(cid, x, y, w, h, label, shape, fill, fontsize=11):
    """Plain AWS resource shape (bucket / queue / role / volume / user ...)."""
    style = (f"sketch=0;outlineConnect=0;fontColor={INK};gradientColor=none;"
             f"fillColor={fill};strokeColor=none;dashed=0;"
             f"verticalLabelPosition=bottom;verticalAlign=top;align=center;"
             f"html=1;fontSize={fontsize};fontStyle=0;aspect=fixed;"
             f"pointerEvents=1;shape=mxgraph.aws4.{shape};")
    vertex(cid, x, y, w, h, label, style)


def grp(cid, x, y, w, h, label, gr_icon, stroke, font, fill="none",
        dashed=0, extra=""):
    style = (f"{_GRP_PREFIX}grIcon=mxgraph.aws4.{gr_icon};"
             f"strokeColor={stroke};fillColor={fill};verticalAlign=top;"
             f"align=left;spacingLeft=34;fontColor={font};dashed={dashed};"
             f"{extra}")
    vertex(cid, x, y, w, h, label, style)


def rect(cid, x, y, w, h, label, fill="none", stroke=GREY, font=INK,
         fontsize=11, dashed=0, bold=0, align="left", valign="top",
         rounded=1, spacing=8, extra=""):
    style = (f"rounded={rounded};arcSize=6;whiteSpace=wrap;html=1;"
             f"fillColor={fill};strokeColor={stroke};fontColor={font};"
             f"fontSize={fontsize};fontStyle={bold};align={align};"
             f"verticalAlign={valign};spacingLeft={spacing};spacingRight={spacing};"
             f"spacingTop=4;dashed={dashed};{extra}")
    vertex(cid, x, y, w, h, label, style)


def label(cid, x, y, w, h, text, fontsize=12, bold=0, color=INK,
          align="left", valign="middle"):
    style = (f"text;html=1;strokeColor=none;fillColor=none;align={align};"
             f"verticalAlign={valign};whiteSpace=wrap;rounded=0;"
             f"fontSize={fontsize};fontStyle={bold};fontColor={color};")
    vertex(cid, x, y, w, h, text, style)


def badge(cid, x, y, n, fill=RUNTIME_BADGE, d=28, fontsize=12):
    style = (f"ellipse;whiteSpace=wrap;html=1;fillColor={fill};"
             f"strokeColor=#ffffff;strokeWidth=2;fontColor=#ffffff;"
             f"fontSize={fontsize};fontStyle=1;align=center;verticalAlign=middle;")
    vertex(cid, x, y, d, d, str(n), style)


def edge(cid, src, tgt, pts=None, text="", color=INK, width=2, dashed=0,
         exit_xy=None, entry_xy=None, fontsize=10, dash_pattern="",
         label_pos=None):
    ex = ""
    if exit_xy:
        ex += (f"exitX={exit_xy[0]};exitY={exit_xy[1]};exitDx=0;exitDy=0;"
               "exitPerimeter=0;")
    if entry_xy:
        ex += (f"entryX={entry_xy[0]};entryY={entry_xy[1]};entryDx=0;entryDy=0;"
               "entryPerimeter=0;")
    dp = f"dashPattern={dash_pattern};" if dash_pattern else ""
    style = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;arcSize=8;"
             f"orthogonalLoop=1;jettySize=auto;html=1;{ex}"
             f"strokeColor={color};strokeWidth={width};endArrow=blockThin;"
             f"endFill=1;dashed={dashed};{dp}jumpStyle=arc;jumpSize=6;"
             f"fontSize={fontsize};fontColor={color};"
             f"labelBackgroundColor=#FFFFFF;")
    body = '<mxGeometry relative="1" as="geometry">'
    if label_pos is not None:
        body += f'\n            <mxPoint as="offset" x="0" y="{label_pos}" />'
    if pts:
        body += '\n            <Array as="points">'
        for px, py in pts:
            body += f'\n              <mxPoint x="{px}" y="{py}" />'
        body += '\n            </Array>'
    body += '\n          </mxGeometry>'
    add(f'<mxCell id="{cid}" value="{esc(text)}" style="{style}" edge="1" '
        f'parent="1" source="{src}" target="{tgt}">\n          {body}\n        </mxCell>')


# =========================================================================== #
# PAGE 1 - AWS ARCHITECTURE
# =========================================================================== #
def page1() -> str:
    cells.clear()
    _boxes.clear()

    label("p1-title", 24, 14, 1900, 34,
          "Radio Broadcast Analysis &mdash; AWS architecture &amp; backend pipeline",
          fontsize=26, bold=1)
    label("p1-sub", 24, 50, 2200, 46,
          "One EC2 instance &middot; 7 Docker Compose services &middot; 2 SQS FIFO queues "
          "&middot; SQLite (WAL) on EBS + S3 &middot; deploy via GitHub OIDC &rarr; SSM (no SSH)<br>"
          "<b>Black badges 1&ndash;22</b> = runtime data pipeline &nbsp;&nbsp;"
          "<b>Blue badges D1&ndash;D6</b> = deployment chain &nbsp;&nbsp;"
          "Every label is taken verbatim from the repository at v0.4.1.",
          fontsize=12)

    # ----------------------------------------------------------------- column A
    label("p1-exthdr", 24, 108, 316, 22, "OUTSIDE AWS", fontsize=12, bold=1,
          color=GREY)

    rect("p1-gh", 24, 136, 316, 252, "GitHub", fill="#F7F8FA",
         stroke=GREY, font=GREY, bold=1, fontsize=13)
    rect("p1-gh1", 44, 172, 276, 46,
         "<b>repository &middot; branch <code>main</code></b><br>"
         "the only deployment source", fill="#FFFFFF", stroke="#D5DBE3", fontsize=10)
    rect("p1-gh2", 44, 224, 276, 46,
         "<b>CI</b> &mdash; ruff &middot; pytest 3.11 &amp; 3.12<br>"
         "bandit -ll &middot; pip-audit", fill="#FFFFFF", stroke="#D5DBE3", fontsize=10)
    rect("p1-gh3", 44, 276, 276, 40,
         "<b>CodeQL</b> &mdash; Analyze Python", fill="#FFFFFF",
         stroke="#D5DBE3", fontsize=10)
    rect("p1-gh4", 44, 322, 276, 52,
         "<b>deploy-main.yml</b><br>workflow_run &rarr; OIDC &rarr; SSM<br>"
         "gate: all 5 checks green on that SHA",
         fill="#FFFFFF", stroke="#D5DBE3", fontsize=10)

    rect("p1-streams", 24, 412, 316, 118,
         "<b>Live radio streams</b><br>"
         "internet stations &middot; MP3 / AAC / HLS / Ogg<br>"
         "URLs are UNTRUSTED &mdash; SSRF-validated on<br>every connect and every redirect hop",
         fill="#FFFFFF", stroke=EXT_STROKE, fontsize=10)
    plain("p1-inet", 246, 432, 78, 48, "", "internet_alt1", GENERAL)

    rect("p1-rb", 24, 550, 316, 118,
         "<b>Radio Browser API</b><br>"
         "community catalogue &middot; mirror pool<br>"
         "SRV discovery &middot; <code>/json/url/&lt;uuid&gt;</code><br>"
         "resolved URL is cached &mdash; asked once per station",
         fill="#FFFFFF", stroke=EXT_STROKE, fontsize=10)

    rect("p1-cons", 24, 688, 316, 176, "Consumers", fill="#F7F8FA",
         stroke=GREY, font=GREY, bold=1, fontsize=13)
    plain("p1-user", 54, 726, 78, 78, "", "user", GENERAL)
    rect("p1-cons1", 148, 726, 172, 46,
         "<b>Dashboard</b><br>browser SPA (CORS)", fill="#FFFFFF",
         stroke="#D5DBE3", fontsize=10)
    rect("p1-cons2", 148, 780, 172, 46,
         "<b>Operator</b><br>curl / Swagger <code>/docs</code>", fill="#FFFFFF",
         stroke="#D5DBE3", fontsize=10)
    label("p1-cons3", 44, 830, 286, 26,
          "pilot is unauthenticated (<code>auth_mode: none</code>) &mdash; "
          "reachable only from the security-group CIDR", fontsize=9, color=SGRED)

    # legend
    rect("p1-leg", 24, 886, 316, 452, "Legend", fill="#F7F8FA", stroke=GREY,
         font=GREY, bold=1, fontsize=13)
    badge("p1-legb1", 44, 922, 7)
    label("p1-legt1", 82, 922, 248, 28,
          "runtime pipeline step (1&ndash;22)", fontsize=10)
    badge("p1-legb2", 44, 958, "D", fill=DEPLOY_BADGE)
    label("p1-legt2", 82, 958, 248, 28, "deployment step (D1&ndash;D6)", fontsize=10)
    label("p1-legl1", 44, 998, 286, 22,
          "<b>&#9473;&#9473;</b> data path (audio, messages, rows)", fontsize=10)
    label("p1-legl2", 44, 1022, 286, 22,
          "<b>&#9476;&#9476;</b> control / deploy path", fontsize=10, color=DEPLOY_BADGE)
    label("p1-legl3", 44, 1046, 286, 22,
          "<b>&#9473;&#9473;</b> S3 export / read-back", fontsize=10, color=STORAGE)
    rect("p1-legtx", 44, 1078, 286, 60,
         "<b>ONE TXN</b> marks a single SQLite transaction. Business rows and the "
         "outbox row commit together &mdash; that is the whole reliability argument "
         "(ADR-009).", fill="#FFFFFF", stroke="#D5DBE3", fontsize=9)
    rect("p1-legsec", 44, 1146, 286, 76,
         "<b>Every container:</b> non-root &middot; <code>cap_drop: ALL</code> &middot; "
         "<code>no-new-privileges</code> &middot; read-only root fs &middot; tmpfs "
         "<code>/tmp</code> &middot; <code>pids: 256</code> &middot; rotated json-file logs. "
         "Never privileged, never host networking, never the Docker socket.",
         fill="#FFFFFF", stroke="#D5DBE3", fontsize=9)
    rect("p1-legcap", 44, 1230, 286, 96,
         "<b>Capacity &mdash; four different numbers</b><br>"
         "catalogue stations 1,000+<br>"
         "<b>requested</b> unique stations 1,000 (load-proven)<br>"
         "<b>active</b> unique stations 1 by code default; 512 in the prod template "
         "with <code>RADIO_ALLOW_UNBENCHMARKED_CAPACITY=1</code><br>"
         "real-time ASR throughput <b>never benchmarked</b>",
         fill="#FFFFFF", stroke="#D5DBE3", fontsize=9)

    rect("p1-facts", 24, 1358, 316, 542, "Runtime facts", fill="#F7F8FA",
         stroke=GREY, font=GREY, bold=1, fontsize=13)
    rect("p1-f1", 44, 1394, 276, 88,
         "<b>Host</b><br>EC2 Graviton, aarch64, 8 vCPU / 16 GiB<br>"
         "(c7g / c8g.2xlarge class)<br>"
         "deploy refuses any non-aarch64 host<br>"
         "instance role supplies AWS credentials",
         fill="#FFFFFF", stroke="#D5DBE3", fontsize=9)
    rect("p1-f2", 44, 1490, 276, 132,
         "<b>Container limits (compose.prod.yaml)</b><br>"
         "api 1.0 CPU / 512 MiB<br>"
         "planner 0.5 / 256<br>"
         "listener 1.5 / 1024<br>"
         "transcription-worker 3.0 / 3072<br>"
         "analysis-worker 0.75 / 512<br>"
         "cleanup-worker 0.25 / 256<br>"
         "llm 3.0 / 4096 &nbsp;&mdash;&nbsp; total 9,728 MiB",
         fill="#FFFFFF", stroke="#D5DBE3", fontsize=9)
    rect("p1-f3", 44, 1630, 276, 116,
         "<b>Models (never in an image, never auto-downloaded)</b><br>"
         "ASR <code>Systran/faster-whisper-small</code><br>"
         "&nbsp;&nbsp;CTranslate2, int8, CPU, beam 1<br>"
         "VAD <code>silero_vad.onnx</code><br>"
         "LLM <code>Qwen3-0.6B-Q8_0.gguf</code><br>"
         "pinned + digest-verified via models.lock.json",
         fill="#FFFFFF", stroke="#D5DBE3", fontsize=9)
    rect("p1-f4", 44, 1754, 276, 130,
         "<b>Known gaps &mdash; documented, not defects</b><br>"
         "&bull; ASR <b>pass B</b> exists and is unit-tested but <b>no worker calls it</b>, "
         "so fuzzy/phonetic candidates are never re-decoded<br>"
         "&bull; <code>_publish_backlog</code> logs only; it does not re-publish<br>"
         "&bull; nothing writes <code>campaign_content_policies</code><br>"
         "&bull; <code>temp-transcripts/</code> and <code>config/</code> prefixes are dead settings",
         fill="#FFF6F6", stroke=SGRED, fontsize=9)

    # ----------------------------------------------------------------- AWS cloud
    grp("p1-cloud", 370, 110, 2150, 1840, "AWS Cloud",
        "group_aws_cloud_alt", INK, INK)
    grp("p1-region", 400, 155, 2090, 1770, "Region &mdash; eu-north-1 (default)",
        "group_region", "#00A4A6", "#147EBA", dashed=1)

    # ------------------------------------------------------------ lane D deploy
    rect("p1-laned", 430, 200, 2030, 210,
         "Deployment &mdash; GitHub OIDC &rarr; fixed SSM document &rarr; immutable release "
         "&nbsp;|&nbsp; no SSH, no port 22, no EC2 key pair, no static AWS credential",
         fill="#F5F5FE", stroke=DEPLOY_BADGE, font=DEPLOY_BADGE, bold=1,
         fontsize=13, dashed=1)

    d_items = [
        ("d1", 520, "tile", "identity_and_access_management", SECURITY, 244,
         "<b>IAM &middot; OIDC provider</b><br>"
         "<code>token.actions.githubusercontent.com</code><br>"
         "ClientId <code>sts.amazonaws.com</code>"),
        ("d2", 830, "role", "role", SECURITY, 261,
         "<b>GitHubActionsRadioDeployRole</b><br>"
         "sts:AssumeRoleWithWebIdentity<br>"
         "sub pinned to <code>&hellip;:environment:production</code><br>"
         "DENY StartSession / RunShellScript / iam:*"),
        ("d3", 1140, "tile", "systems_manager", APPINT, 244,
         "<b>AWS Systems Manager</b><br>"
         "ssm:SendCommand only<br>"
         "max-concurrency 1 &middot; max-errors 0"),
        ("d4", 1450, "documents", "documents", APPINT, 244,
         "<b>RadioBroadcastDeployMain</b><br>"
         "one parameter: <code>CommitSha ^[0-9a-f]{40}$</code><br>"
         "numeric version pinned &mdash; never $LATEST"),
        ("d5", 1760, "tile", "ec2", COMPUTE, 244,
         "<b>SSM Agent on the EC2 host</b><br>"
         "git fetch only &middot; merge-base --is-ancestor origin/main<br>"
         "git archive &rarr; <code>main-auto-deploy.sh</code> (9 stages)"),
        ("d6", 2120, "tile", "elastic_block_store", STORAGE, 244,
         "<b>Immutable release</b><br>"
         "<code>/var/lib/radio/releases/&lt;sha&gt;/&lt;stage&gt;</code><br>"
         "stages api &rarr; core &rarr; full &middot; symlinks current / previous"),
    ]
    for i, (cid, x, kind, shape, colour, iy, txt) in enumerate(d_items, start=1):
        if kind == "tile":
            tile(f"p1-{cid}", x, iy, "", shape, colour)
        elif kind == "role":
            plain(f"p1-{cid}", x, iy, 78, 44, "", shape, colour)
        else:
            plain(f"p1-{cid}", x + 7, iy, 64, 78, "", shape, colour)
        rect(f"p1-{cid}lbl", x - 80, 328, 238, 74, txt, fill="#FFFFFF",
             stroke="#D5DBE3", fontsize=9)
        badge(f"p1-{cid}b", x - 34, 246, f"D{i}", fill=DEPLOY_BADGE, fontsize=10)

    for i in range(1, 6):
        edge(f"p1-de{i}", f"p1-d{i}", f"p1-d{i+1}", color=DEPLOY_BADGE,
             width=2, dashed=1, dash_pattern="6 4",
             exit_xy=(1, 0.5), entry_xy=(0, 0.5))

    # ----------------------------------------------------------------- VPC / EC2
    grp("p1-vpc", 430, 445, 1632, 790,
        "VPC &mdash; single AZ, no NAT gateway, no load balancer",
        "group_vpc2", NETWORK, "#8C4FFF")
    grp("p1-subnet", 458, 476, 1574, 734, "Public subnet",
        "group_security_group", "#7AA116", "#248814", fill="#F8FBF2",
        extra="grStroke=0;")
    rect("p1-sg", 484, 518, 1520, 672,
         "Security group &mdash; inbound tcp/8788 from the operator CIDR only "
         "(API bound to 127.0.0.1 by default; 0.0.0.0 needs two explicit flags)",
         fill="none", stroke=SGRED, font=SGRED, bold=1, fontsize=12, rounded=0,
         spacing=10)
    grp("p1-ec2", 508, 570, 1470, 596,
        "EC2 instance &mdash; Graviton aarch64, 8 vCPU / 16 GiB &middot; "
        "EBS data volume mounted at /var/lib/radio",
        "group_ec2_instance_contents", EC2ORANGE, EC2ORANGE)
    rect("p1-compose", 528, 612, 1230, 512,
         "Docker Compose project <code>radio-prod</code> &mdash; bridge network "
         "<code>radio</code>",
         fill="#FFFFFF", stroke="#D5DBE3", font=GREY, bold=1, fontsize=12)

    svc = [
        # id      x     y   w    h   title / body                              badge
        ("listener", 548, 656, 372, 244, COMPUTE,
         "listener",
         "one async session per <b>DISTINCT</b> station<br>"
         "&#9679; SSRF re-validate &rarr; ffmpeg &rarr; 16 kHz mono s16le<br>"
         "&nbsp;&nbsp;<code>-reconnect 0</code>, <code>-protocol_whitelist</code> "
         "(no file/concat)<br>"
         "&#9679; ring buffer 60 s = 1,920,000 B / station<br>"
         "&nbsp;&nbsp;timestamps from sample count, not wall clock<br>"
         "&#9679; VAD + energy classifier, 3 s windows<br>"
         "&nbsp;&nbsp;silence / music / singing discarded <b>in RAM</b><br>"
         "&nbsp;&nbsp;uncertain is KEPT (recall &gt; CPU)<br>"
         "&#9679; 20 s segments, 1 s overlap &rarr; Opus 24k voip<br>"
         "&#9679; spool write: tmp &rarr; fsync &rarr; os.replace &rarr; sha256<br>"
         "<i>1.5 CPU &middot; 1024 MiB &middot; stop_grace 30 s</i>"),
        ("transcription", 958, 656, 372, 244, COMPUTE,
         "transcription-worker",
         "&#9679; receive &le;5 &middot; long-poll 20 s<br>"
         "&nbsp;&nbsp;visibility 300 s, heartbeat every 60 s, cap 1800 s<br>"
         "&#9679; inbox pre-check &rarr; duplicate deleted, not decoded<br>"
         "&#9679; job older than 6 h &rarr; <code>abandoned</code>, no decode<br>"
         "&#9679; <b>SHA-256 verified before any decoder sees bytes</b><br>"
         "&#9679; ASR pass A &mdash; faster-whisper small, int8, beam 1<br>"
         "&#9679; Aho-Corasick scan <b>once</b> vs the station's<br>"
         "&nbsp;&nbsp;COMBINED index &rarr; every campaign attributed<br>"
         "&#9679; conversation assembler: 30 s pre-roll, closes on<br>"
         "&nbsp;&nbsp;12 s gap / 300 s / disconnect / shutdown<br>"
         "<i>3.0 CPU &middot; 3072 MiB &middot; 3 ASR threads</i>"),
        ("analysis", 1368, 656, 370, 244, COMPUTE,
         "analysis-worker",
         "&#9679; receive exactly <b>1</b> (LLM is serial on CPU)<br>"
         "&#9679; conversation reloaded from SQLite &mdash; transcripts<br>"
         "&nbsp;&nbsp;travel by <b>reference</b>, never inside a message<br>"
         "&#9679; LLM chain &rarr; validated JSON; evidence must be a<br>"
         "&nbsp;&nbsp;<b>verbatim transcript substring</b> or it is dropped<br>"
         "&#9679; fan-out 1 mention : N campaigns : N keywords<br>"
         "&#9679; evidence clip cut from retained spool segments<br>"
         "&#9679; idle sweeps: clip backfill (3/tick) and<br>"
         "&nbsp;&nbsp;fallback re-analysis healing (2/tick)<br>"
         "&#9679; never fails a message &mdash; degrades to fallback<br>"
         "<i>0.75 CPU &middot; 512 MiB &middot; tmpfs /tmp 256 MiB</i>"),
    ]
    for cid, x, y, w, h, colour, title, body in svc:
        rect(f"p1-{cid}", x, y, w, h, "", fill="#FFF8F2", stroke=COMPUTE)
        tile(f"p1-{cid}i", x + 12, y + 10, "", "ecs", colour, size=36)
        label(f"p1-{cid}t", x + 56, y + 12, w - 66, 26, f"<b>{title}</b>",
              fontsize=13)
        label(f"p1-{cid}b", x + 12, y + 48, w - 24, h - 56, body, fontsize=9)

    svc2 = [
        ("api", 548, 936, 268, 104,
         "api",
         "FastAPI / uvicorn <b>:8788</b> &mdash; the only<br>"
         "published port (loopback by default)<br>"
         "campaigns &middot; mentions &middot; dashboard &middot; catalogue<br>"
         "monitoring &middot; preview &middot; HMAC audio token<br>"
         "applies schema migrations at lifespan<br>"
         "<i>1.0 CPU &middot; 512 MiB</i>"),
        ("planner", 856, 936, 268, 104,
         "planner",
         "every 5 s: campaigns &rarr; one subscription<br>"
         "per DISTINCT station (BLAKE2b sharding)<br>"
         "publishes the combined keyword index<br>"
         "<b>the ONLY process that sends to SQS</b><br>"
         "sweeps stale job leases + prunes tables<br>"
         "<i>0.5 CPU &middot; 256 MiB</i>"),
        ("llm", 1164, 936, 268, 104,
         "llm",
         "llama.cpp <code>llama-server</code> <b>:8790</b><br>"
         "Qwen3-0.6B-Q8_0.gguf<br>"
         "GBNF grammar-constrained JSON<br>"
         "<b>expose only &mdash; never published</b><br>"
         "only volume: /models (read-only)<br>"
         "<i>3.0 CPU &middot; 4096 MiB</i>"),
        ("cleanup", 1472, 936, 266, 104,
         "cleanup-worker",
         "every 60 s: deletes ONLY segments whose<br>"
         "SQLite job state proves it safe<br>"
         "<code>pending</code> and <code>retained</code> are never deleted,<br>"
         "at any watermark &mdash; batch 200<br>"
         "prunes inbox + deleted segment rows<br>"
         "<i>0.25 CPU &middot; 256 MiB</i>"),
    ]
    for cid, x, y, w, h, title, body in svc2:
        rect(f"p1-{cid}", x, y, w, h, "", fill="#FFF8F2", stroke=COMPUTE)
        tile(f"p1-{cid}i", x + 10, y + 8, "", "ecs", COMPUTE, size=28)
        label(f"p1-{cid}t", x + 44, y + 8, w - 52, 22, f"<b>{title}</b>",
              fontsize=12)
        label(f"p1-{cid}b", x + 10, y + 38, w - 20, h - 44, body, fontsize=8)

    # EBS storage column
    rect("p1-store", 1790, 612, 172, 388,
         "EBS gp3<br>/var/lib/radio",
         fill="#F4F8EC", stroke=STORAGE, font="#248814", bold=1, fontsize=11,
         align="center", spacing=2)
    vols = [
        ("db", "database/<br>radio.db<br><i>SQLite WAL</i>"),
        ("spool", "spool/<br><i>Opus segments</i>"),
        ("models", "models/<br><i>&rarr; /models :ro</i>"),
        ("evid", "evidence/"),
        ("logs", "logs/ &middot; backups/<br>releases/"),
    ]
    for i, (vid, txt) in enumerate(vols):
        vy = 662 + i * 66
        plain(f"p1-v{vid}", 1806, vy, 34, 43, "", "volume", STORAGE, fontsize=8)
        label(f"p1-v{vid}t", 1846, vy - 4, 110, 52, txt, fontsize=8)

    # ------------------------------------------------------------- right column
    rect("p1-gaps", 2090, 445, 380, 165,
         "<b>Not deployed today</b> &mdash; verified absent from the repo, listed so "
         "nobody assumes otherwise<br>"
         "&#10007; ALB / NLB &nbsp; &#10007; Route 53 &nbsp; &#10007; ACM / TLS<br>"
         "&#10007; CloudFront &nbsp; &#10007; API Gateway<br>"
         "&#10007; CloudWatch agent, Logs or metrics<br>"
         "&#10007; Lambda &middot; DynamoDB &middot; RDS &middot; ECS / EKS / ECR<br>"
         "&#10007; Secrets Manager / Parameter Store &nbsp; &#10007; KMS<br>"
         "&#10007; Auto Scaling &nbsp; &#10007; multi-AZ<br>"
         "TLS termination is deferred work; logs go to the Docker "
         "<code>json-file</code> driver on EBS.",
         fill="#FFFFFF", stroke=GREY, font=GREY, fontsize=9, dashed=1)

    rect("p1-s3", 2090, 640, 380, 612,
         "Amazon S3 &mdash; one bucket (RADIO_S3_BUCKET)",
         fill="#F4F8EC", stroke=STORAGE, font="#248814", bold=1, fontsize=12)
    tile("p1-s3i", 2118, 676, "", "s3", STORAGE, size=58)
    label("p1-s3n", 2190, 678, 262, 56,
          "SSE-S3 <code>AES256</code> on every put<br>"
          "deterministic keys &rarr; a retry overwrites<br>"
          "<b>presigned URLs are never generated</b>", fontsize=9)
    prefixes = [
        ("mentions/YYYY/MM/DD/&lt;id&gt;/", "metadata + transcript + analysis JSON", 1),
        ("evidence/YYYY/MM/DD/&lt;id&gt;.opus", "the playable mention clip", 1),
        ("results/conversation-analysis/", "legacy v0.4 analysis documents", 1),
        ("results/semantic-matches/", "cross-language semantic hits", 1),
        ("config/keywords/keywords.json", "published entity/keyword document", 1),
        ("backups/sqlite/", "hardcoded in backup-sqlite.sh", 1),
        ("temp-speech/", "only when RADIO_SEGMENT_STORE=s3", 0),
        ("results/intelligence/", "READ / LIST only &mdash; never written here", 0),
        ("transcripts/", "READ / LIST only (semantic scan)", 0),
        ("raw-audio/", "legacy; role deliberately lacks ListBucket", 0),
    ]
    for i, (key, note, live) in enumerate(prefixes):
        py = 746 + i * 46
        rect(f"p1-p{i}", 2112, py, 336, 40,
             f"<b>{key}</b><br>{note}",
             fill="#FFFFFF" if live else "#F7F8FA",
             stroke="#D5DBE3" if live else GREY, fontsize=8,
             dashed=0 if live else 1)
    label("p1-s3f", 2112, 1206, 336, 34,
          "SQLite is the system of record; S3 is an <b>export</b>. A row with no "
          "object is retryable; an object with no row is invisible.", fontsize=8)

    # hosted LLM tiers (outside AWS, right)
    rect("p1-hosted", 2550, 300, 320, 280,
         "Hosted LLM tiers &mdash; optional, OFF by default",
         fill="#FFFFFF", stroke=EXT_STROKE, font=GREY, bold=1, fontsize=12)
    tiers = ["NVIDIA &nbsp;<i>nemotron-3.5-lightning-30b-a3b</i>",
             "Ollama cloud &nbsp;<i>gemma4:31b</i>",
             "Groq &nbsp;<i>qwen/qwen3.6-27b</i>",
             "Mistral &nbsp;<i>ministral-8b-latest</i>",
             "Gemini &nbsp;<i>gemini-flash-latest</i>"]
    for i, t in enumerate(tiers):
        rect(f"p1-t{i}", 2570, 336 + i * 36, 280, 30,
             f"<b>{i+1}.</b> {t}", fill="#F7F8FA", stroke="#D5DBE3", fontsize=9)
    label("p1-hostedn", 2570, 534, 280, 44,
          "Any error &mdash; or a 200 with unparseable content &mdash; cascades to the "
          "next tier <b>inside the same call</b>; the failed tier rests 2 h. "
          "The local model is the end of the chain.", fontsize=8)

    # ------------------------------------------------------------------ SQS lane
    rect("p1-sqs", 430, 1270, 2030, 250,
         "Amazon SQS &mdash; two FIFO queues (metadata and references only; never "
         "audio bytes, never a transcript body, never a credential)",
         fill="#FFF5FA", stroke=APPINT, font=APPINT, bold=1, fontsize=13)
    tile("p1-sqsi", 460, 1322, "", "sqs", APPINT, size=58)
    label("p1-sqsit", 452, 1386, 100, 40, "Amazon SQS", fontsize=9,
          align="center")

    rect("p1-q1", 570, 1318, 920, 128, "", fill="#FFFFFF", stroke="#F0B6D2")
    plain("p1-q1i", 592, 1352, 78, 47, "", "queue", APPINT)
    label("p1-q1t", 686, 1328, 790, 112,
          "<b>RADIO_TRANSCRIPTION_QUEUE_URL</b> &nbsp;<i>&hellip;/&lt;name&gt;.fifo</i><br>"
          "schema <code>radio.transcription.v1</code> &middot; body &le; 64 KiB "
          "(self-imposed, ~16&times; below the SQS limit)<br>"
          "<b>MessageGroupId = station_id</b> &rarr; a station's segments stay ordered, "
          "one in flight per station<br>"
          "<b>MessageDeduplicationId = segment_id</b><br>"
          "producer: <b>planner</b> outbox dispatcher &nbsp;&middot;&nbsp; consumer: "
          "<b>transcription-worker</b> (&le;5 per receive)", fontsize=9)

    rect("p1-q2", 1510, 1318, 920, 128, "", fill="#FFFFFF", stroke="#F0B6D2")
    plain("p1-q2i", 1532, 1352, 78, 47, "", "queue", APPINT)
    label("p1-q2t", 1626, 1328, 790, 112,
          "<b>RADIO_ANALYSIS_QUEUE_URL</b> &nbsp;<i>&hellip;/&lt;name&gt;.fifo</i><br>"
          "schema <code>radio.analysis.v1</code> &middot; carries the matched-keyword "
          "evidence, not the transcript<br>"
          "<b>MessageGroupId = station_id</b><br>"
          "<b>MessageDeduplicationId = analysis_job_id</b><br>"
          "producer: <b>planner</b> outbox dispatcher &nbsp;&middot;&nbsp; consumer: "
          "<b>analysis-worker</b> (hardcoded 1 per receive)", fontsize=9)

    rect("p1-sqsn", 460, 1458, 1970, 48,
         "Startup refuses a URL that is not <code>https</code> and does not end in "
         "<code>.fifo</code> &mdash; per-station ordering is a correctness requirement. "
         "FIFO deduplication lasts only 5 minutes, so it is <b>defence in depth</b>; the "
         "<code>inbox_messages</code> table is the actual guarantee. No dead-letter code exists: "
         "redrive is a queue attribute owned by infrastructure, and a message whose failure "
         "is already understood is recorded in <code>processing_failures</code> and deleted.",
         fill="#FFFFFF", stroke="#F0B6D2", fontsize=9)

    # ------------------------------------------------------------- SQLite lane
    rect("p1-sql", 430, 1550, 2030, 190,
         "SQLite (WAL) on EBS &mdash; the system of record. Every connection gets four "
         "pragmas: journal_mode=WAL, foreign_keys=ON, busy_timeout=30000, "
         "synchronous=NORMAL. Migrations are forward-only (0003&ndash;0007).",
         fill="#FBF3FC", stroke=DATABASE, font=DATABASE, bold=1, fontsize=13)
    tables = [
        ("Campaign intent",
         "campaigns<br>campaign_stations<br>campaign_keywords<br>"
         "campaign_content_policies <i>(no writer)</i>"),
        ("Planner output",
         "station_subscriptions<br>station_keyword_index_versions<br>"
         "station_keyword_bindings"),
        ("Capture &amp; ASR",
         "station_sessions<br>audio_segments<br>transcription_jobs<br>transcripts"),
        ("Analysis &amp; attribution",
         "conversation_sessions<br>mention_events <i>(no campaign_id column)</i><br>"
         "mention_campaigns &middot; mention_keywords<br>analysis_results"),
        ("Reliability",
         "outbox_events<br>inbox_messages<br>worker_heartbeats<br>processing_failures"),
        ("Catalogue (v0.4)",
         "managed_stations<br>radio_catalog_overrides / _deletions<br>"
         "station_jobs &middot; station_probe_results<br>campaign_station_members"),
    ]
    for i, (title, body) in enumerate(tables):
        tx = 452 + i * 335
        rect(f"p1-tb{i}", tx, 1602, 310, 118, "", fill="#FFFFFF",
             stroke="#E5C7EA")
        label(f"p1-tb{i}t", tx + 10, 1608, 290, 20, f"<b>{title}</b>",
              fontsize=10, color=DATABASE)
        label(f"p1-tb{i}b", tx + 10, 1630, 290, 84, body, fontsize=9)

    # --------------------------------------------------------- reliability lane
    rect("p1-rel", 430, 1770, 2030, 130,
         "Reliability model &mdash; how work survives a crash, a redelivery or a "
         "wedged dependency",
         fill="#F7F8FA", stroke=GREY, font=GREY, bold=1, fontsize=13)
    rels = [
        ("Transactional outbox",
         "Producers never call SQS. They INSERT into <code>outbox_events</code> inside the "
         "same transaction as the business rows; the <b>planner</b> is the single "
         "dispatcher. Closes the \"committed but never queued\" silent stall."),
        ("Consumer inbox",
         "Business result + <code>inbox_messages</code> row commit together, <b>then</b> the "
         "SQS message is deleted. A crash in between redelivers, and the inbox turns the "
         "redelivery into a no-op. Every insert is UNIQUE-guarded."),
        ("Leases &amp; heartbeats",
         "Visibility 300 s extended every 60 s up to 1800 s. <code>worker_heartbeats</code> "
         "lets <code>/readyz</code> tell \"never ran\" from \"died 4 minutes ago\". A stale-job "
         "sweeper reclaims work whose worker was killed mid-flight."),
        ("Error taxonomy",
         "Retryability is a property of the exception <i>type</i>, not a per-call-site "
         "decision. Retryable &rarr; leave the message. Permanent &rarr; record in "
         "<code>processing_failures</code>, write the inbox row, delete the message."),
    ]
    for i, (title, body) in enumerate(rels):
        rx = 452 + i * 502
        rect(f"p1-rl{i}", rx, 1812, 480, 76, "", fill="#FFFFFF",
             stroke="#D5DBE3")
        label(f"p1-rl{i}t", rx + 10, 1816, 460, 18, f"<b>{title}</b>",
              fontsize=10)
        label(f"p1-rl{i}b", rx + 10, 1836, 460, 48, body, fontsize=8)

    # ------------------------------------------------------------------- edges
    E = INK
    # 1 dashboard -> api
    edge("p1-e1", "p1-cons", "p1-api", pts=[(470, 777), (470, 988)],
         text="REST / JSON  tcp 8788", color=E, exit_xy=(1, 0.505),
         entry_xy=(0, 0.5))
    badge("p1-b1", 476, 930, 1)

    # 4 radio browser -> planner
    edge("p1-e4", "p1-rb", "p1-planner", pts=[(444, 612), (444, 906), (990, 906)],
         color=E, exit_xy=(1, 0.525), entry_xy=(0.5, 0),
         text="resolve stream URL")
    badge("p1-b4", 448, 636, 4)

    # 6 streams -> listener
    edge("p1-e6", "p1-streams", "p1-listener", pts=[(414, 472), (414, 778)],
         color=E, width=3, exit_xy=(1, 0.508), entry_xy=(0, 0.5),
         text="live audio")
    badge("p1-b6", 418, 496, 6)

    # 10 planner -> SQS
    edge("p1-e10", "p1-planner", "p1-q1", pts=[(990, 1180)], color=E, width=3,
         exit_xy=(0.5, 1), entry_xy=(0.456, 0))
    badge("p1-b10", 996, 1180, 10)
    label("p1-l10", 1004, 1238, 320, 30,
          "<b>outbox dispatcher</b> &rarr; send_message()<br>"
          "the only egress to SQS in the system", fontsize=9)

    # 11 SQS q1 -> transcription
    edge("p1-e11", "p1-q1", "p1-transcription", pts=[(1144, 1240)], color=E,
         width=3, exit_xy=(0.624, 0), entry_xy=(0.5, 1))
    badge("p1-b11", 1150, 1180, 11)

    # 16 SQS q2 -> analysis
    edge("p1-e16", "p1-q2", "p1-analysis", pts=[(1560, 1250), (1452, 1250)],
         color=E, width=3, exit_xy=(0.054, 0), entry_xy=(0.227, 1))
    badge("p1-b16", 1458, 1180, 16)

    # 18 analysis -> llm
    edge("p1-e18", "p1-analysis", "p1-llm", pts=[(1420, 918), (1300, 918)],
         color=E, exit_xy=(0.14, 1), entry_xy=(0.508, 0))
    badge("p1-b18", 1286, 904, 18)

    # 18b analysis -> hosted tiers
    edge("p1-e18b", "p1-analysis", "p1-hosted",
         pts=[(1560, 430), (2710, 430)], color=E, dashed=1, dash_pattern="8 4",
         exit_xy=(0.519, 0), entry_xy=(0.5, 1),
         text="tier chain, tried best-first, before the local model")

    # 8/12/21 compose <-> EBS
    edge("p1-e8", "p1-compose", "p1-store", color=E, width=3,
         exit_xy=(1, 0.367), entry_xy=(0, 0.484))
    badge("p1-b8", 1748, 754, 8)
    label("p1-l8", 1596, 1048, 180, 60,
          "<b>8</b> write segment (fsync+rename)<br>"
          "<b>12</b> read + verify SHA-256<br>"
          "<b>21</b> cleanup deletes by job state", fontsize=8)

    # 20 analysis -> S3
    edge("p1-e20", "p1-analysis", "p1-s3",
         pts=[(1774, 840), (1774, 1080)], color=STORAGE, width=3,
         exit_xy=(1, 0.754), entry_xy=(0, 0.719))
    badge("p1-b20", 1786, 1046, 20)

    # 22 api <-> S3
    edge("p1-e22", "p1-api", "p1-s3", pts=[(700, 1100), (2050, 1100)],
         color=STORAGE, width=2, dashed=1, dash_pattern="8 4",
         exit_xy=(0.567, 1), entry_xy=(0, 0.7516))
    badge("p1-b22", 1900, 1086, 22)
    label("p1-l22", 700, 1104, 420, 20,
          "keywords.json put &middot; evidence clip Range-streamed back to the browser",
          fontsize=8, color="#4C7A0B")

    # D5 -> EC2
    edge("p1-ed5", "p1-d5", "p1-ec2", pts=[(1960, 283)], color=DEPLOY_BADGE,
         dashed=1, dash_pattern="6 4", exit_xy=(1, 0.5), entry_xy=(0.987, 0))
    # GitHub -> D1
    edge("p1-egh", "p1-gh", "p1-d1", color=DEPLOY_BADGE, dashed=1,
         dash_pattern="6 4", exit_xy=(1, 0.6), entry_xy=(0, 0.5))

    return "\n".join(cells)


# =========================================================================== #
# PAGE 2 - NUMBERED PIPELINE
# =========================================================================== #
PHASES = [
    ("A", "Intent &mdash; the control plane records what to watch", "#EEF3FB", "#2E5AAC", [
        (1, "browser &rarr; api",
         "<code>POST /api/v1/brand-signal/campaigns</code>. Station ids are validated against the "
         "station map; <code>backfill_days</code> becomes <code>monitor_from</code>. The API records "
         "<b>intent only</b> &mdash; it never opens a stream.",
         "&mdash;"),
        (2, "api &rarr; SQLite + S3",
         "<b>ONE TXN:</b> 1 &times; <code>campaigns</code> (status=active) + N &times; "
         "<code>campaign_stations</code> + N &times; <code>campaign_keywords</code>, then a revision bump. "
         "The merged entity document is put to S3 and an immediate sync runs.",
         "campaigns, campaign_stations,<br>campaign_keywords<br>"
         "s3://&hellip;/config/keywords/keywords.json"),
    ]),
    ("B", "Planning &mdash; campaigns become stations, de-duplicated", "#F3EEFB", "#6B3FBF", [
        (3, "planner (every 5 s)",
         "Active campaigns are grouped by <b>DISTINCT</b> station. One "
         "<code>station_subscriptions</code> row per station carries "
         "<code>reference_count</code> (how many campaigns want it) and "
         "<code>shard_index = BLAKE2b(station_id) % shards</code> &mdash; never Python's "
         "<code>hash()</code>, which is per-process randomised and would split-brain two listeners.",
         "station_subscriptions"),
        (4, "planner &rarr; Radio Browser",
         "A campaign stores a station id; the listener needs a URL. Resolution is durable-first "
         "(<code>managed_stations.stream_url_resolved</code>), then <code>/json/url/&lt;uuid&gt;</code>. "
         "Budgeted per cycle and backed off on failure &mdash; that endpoint counts a click.",
         "station_subscriptions.stream_url"),
        (5, "planner",
         "Builds the <b>combined</b> keyword index for the station from every campaign that "
         "references it and publishes a new version only when the content fingerprint changed. "
         "Then admits up to <code>RADIO_MAX_ACTIVE_UNIQUE_STATIONS</code>; the overflow is parked "
         "as <code>pending_capacity</code> with a stated reason &mdash; a queue, never a silent drop.",
         "station_keyword_index_versions,<br>station_keyword_bindings"),
    ]),
    ("C", "Capture &mdash; the only place audio exists", "#FFF4EC", "#C4590C", [
        (6, "listener &rarr; ffmpeg",
         "The URL is <b>re-validated on every connect</b> (DNS can be re-pointed between "
         "configuration and use). ffmpeg runs with <code>-reconnect 0</code> so every reconnect "
         "re-runs that check, and a <code>-protocol_whitelist</code> that excludes "
         "<code>file</code> and <code>concat</code>. Output: 16 kHz mono s16le.",
         "station_sessions"),
        (7, "listener: ring buffer + classifier",
         "PCM lands in a fixed 1,920,000-byte ring buffer (60 s @ 16 kHz &times; 2 B). Timestamps come "
         "from the <b>sample count</b>, not <code>datetime.now()</code>. A VAD + energy classifier scores "
         "3 s windows: silence, clear music and long-form singing are <b>discarded in RAM</b>; speech, "
         "speech-over-music, jingles and <b>uncertain</b> are kept.",
         "nothing &mdash; discarded audio never<br>touches the disk"),
        (8, "listener: encode + spool",
         "Segments of 20 s with 1 s overlap are encoded to Ogg/Opus 24 kbit/s <code>voip</code> "
         "(lossless WAV fallback), then written tmp &rarr; <code>fsync</code> &rarr; <code>os.replace</code> "
         "&rarr; directory fsync, with SHA-256 computed at write time.",
         "/var/lib/radio/spool/&lt;station&gt;/<br>&lt;segment&gt;.opus"),
        (9, "listener &rarr; SQLite",
         "<b>ONE TXN:</b> <code>audio_segments</code> (disposition=pending, sha256, size) + "
         "<code>transcription_jobs</code> (pending) + <code>outbox_events</code> (queue=transcription). "
         "Bytes land <i>before</i> anything references them; if the transaction fails the file just "
         "written is deleted. Disk watermarks (70/85/90 %) are checked <b>before</b> admitting.",
         "audio_segments, transcription_jobs,<br>outbox_events"),
    ]),
    ("D", "Queue 1 &rarr; transcribe &rarr; match", "#FFF0F7", "#B3106A", [
        (10, "planner &rarr; SQS FIFO 1",
         "The dispatcher claims a batch of 25 with a 120 s lease, sends <b>outside</b> any "
         "transaction, then records the outcome in a second short transaction. "
         "<code>MessageGroupId=station_id</code>, <code>MessageDeduplicationId=segment_id</code>.",
         "outbox_events.status = sent"),
        (11, "transcription-worker &larr; SQS",
         "Receives up to 5 with a 20 s long poll; visibility 300 s extended every 60 s (cap 1800 s). "
         "A known duplicate is deleted without work. A job older than 6 h is marked "
         "<code>abandoned</code> and its audio released &mdash; decoding a day-old backlog starves the "
         "fresh audio behind it.",
         "inbox_messages (pre-check)"),
        (12, "transcription-worker &larr; spool",
         "The path is re-resolved inside the spool root with <code>O_NOFOLLOW</code> and an "
         "<code>st_dev</code>/<code>st_ino</code> TOCTOU check, the size is compared, and the "
         "<b>SHA-256 recorded at write time is verified before any decoder sees the bytes</b>.",
         "&mdash;"),
        (13, "transcription-worker: ASR pass A",
         "faster-whisper <code>small</code>, CTranslate2 int8, <code>beam_size=1</code>, no word "
         "timestamps, language pinned only when exactly one usable hint exists. No-speech and "
         "repetition-loop guards drop bad segments. <b>ONE TXN:</b> transcript + inbox row.",
         "transcripts (asr_pass='a'),<br>inbox_messages"),
        (14, "transcription-worker: match + assemble",
         "One Aho-Corasick scan of the whole station index &mdash; O(len(text)) regardless of keyword "
         "count &mdash; and each hit resolves to <b>every</b> campaign that registered it. Hits feed the "
         "per-station conversation assembler, which reaches back 30 s for context and closes on a "
         "12 s gap, 300 s, disconnect or shutdown.",
         "&mdash; (in-memory state machine)"),
        (15, "transcription-worker &rarr; SQLite",
         "<b>ONE TXN:</b> <code>conversation_sessions</code> (closed) + stamp "
         "<code>transcripts.conversation_id</code> + <code>outbox_events</code> (queue=analysis). "
         "Only after this commits is the SQS message deleted. Then two small transactions mark the "
         "job succeeded and set <code>disposition</code> to <b>retained</b> (matched) or "
         "<b>disposable</b> (no match) &mdash; the flag cleanup keys on.",
         "conversation_sessions, transcripts,<br>outbox_events, audio_segments"),
    ]),
    ("E", "Queue 2 &rarr; analyse &rarr; durable mention", "#F0F7FF", "#0B6AA8", [
        (16, "planner &rarr; SQS FIFO 2",
         "The same single dispatcher sends the analysis job. "
         "<code>MessageGroupId=station_id</code>, "
         "<code>MessageDeduplicationId=analysis_job_id</code>. The message carries the matched-keyword "
         "evidence but <b>not</b> the transcript.",
         "outbox_events.status = sent"),
        (17, "analysis-worker &larr; SQS",
         "Exactly one message per receive &mdash; LLM work is serial on CPU and batching only adds "
         "latency. The conversation is reloaded from SQLite, so the analysed text is the committed "
         "text. A missing row is <i>retryable</i>; zero matched keywords is <i>permanent</i>.",
         "&mdash;"),
        (18, "analysis-worker &rarr; LLM",
         "Chain, best first: NVIDIA &rarr; Ollama &rarr; Groq &rarr; Mistral &rarr; Gemini &rarr; local "
         "<code>llama-server</code>. Any error, or a 200 whose body has no parseable JSON, cascades "
         "within the same call and rests that tier for 2 h. Output is untrusted: "
         "<b>every evidence quote must appear verbatim in the transcript</b> or it is dropped, "
         "enumerations are coerced, <code>&lt;think&gt;</code> blocks are stripped, and a failure "
         "degrades to a deterministic fallback rather than losing the mention.",
         "&mdash;"),
        (19, "analysis-worker &rarr; SQLite",
         "<b>ONE TXN:</b> <code>mention_events</code> (UNIQUE on conversation_id) + one "
         "<code>mention_campaigns</code> row per campaign with its include/exclude verdict + one "
         "<code>mention_keywords</code> row per keyword + <code>analysis_results</code> + the inbox row. "
         "<code>mention_events</code> has <b>no</b> campaign_id column &mdash; that is what makes "
         "\"analyse once, attribute many times\" true by construction.",
         "mention_events, mention_campaigns,<br>mention_keywords, analysis_results,<br>inbox_messages"),
        (20, "analysis-worker &rarr; S3",
         "Outside the transaction: three objects under "
         "<code>mentions/YYYY/MM/DD/&lt;mention_id&gt;/</code> &mdash; metadata, transcript, analysis "
         "&mdash; with SSE-S3, then <code>result_s3_key</code> is recorded. Allowed to fail: SQLite "
         "already holds what the API serves.",
         "s3 mentions/&hellip;<br>mention_events.result_s3_key"),
        (21, "analysis-worker &rarr; spool + S3",
         "The conversation's retained segments are read back (a <b>second</b> SHA-256 verification), "
         "stream-copied when formats match or re-encoded to Opus otherwise, and uploaded as one clip. "
         "<code>evidence_available</code> flips to 1, which is what makes the audio routes work.",
         "s3 evidence/YYYY/MM/DD/&lt;id&gt;.opus<br>mention_events.evidence_storage_key"),
    ]),
    ("F", "Retention and serving", "#F2F7EC", "#4C7A0B", [
        (22, "cleanup-worker &nbsp;+&nbsp; api &rarr; browser",
         "<b>Cleanup</b> deletes only what SQLite proves is safe: disposition <code>disposable</code> "
         "past 10 min with the job succeeded/abandoned, or <code>failed</code> past 24 h &mdash; and "
         "never a segment a mention depends on. <code>pending</code> and <code>retained</code> are "
         "never deleted at any watermark. &nbsp; <b>API</b> serves "
         "<code>/dashboard</code> and <code>/mentions</code>, then mints an HMAC-SHA256 audio token "
         "(TTL 600 s, key allow-listed) that the browser exchanges for a Range-capable stream of the "
         "clip from S3.",
         "audio_segments.disposition='deleted'<br>inbox_messages pruned (7 d)<br>"
         "HTTP 206 audio stream"),
    ]),
]


def page2() -> str:
    cells.clear()
    _boxes.clear()

    label("p2-title", 40, 24, 1700, 34,
          "Backend pipeline &mdash; 22 numbered steps, end to end", fontsize=26,
          bold=1)
    label("p2-sub", 40, 60, 2100, 44,
          "Read top to bottom. <b>ONE TXN</b> marks a single SQLite transaction &mdash; every "
          "row listed there commits together or not at all.<br>"
          "The two SQS hops (steps 10 and 16) are the only places work leaves the instance; "
          "everything else is local to the EC2 host.", fontsize=12)

    hy = 120
    rect("p2-hdr", 40, hy, 2210, 32, "", fill="#232F3E", stroke="none")
    label("p2-h0", 52, hy, 46, 32, "<b>#</b>", fontsize=11, color="#FFFFFF",
          align="center")
    label("p2-h1", 108, hy, 250, 32, "<b>PROCESS</b>", fontsize=11,
          color="#FFFFFF")
    label("p2-h2", 372, hy, 1320, 32, "<b>WHAT HAPPENS</b>", fontsize=11,
          color="#FFFFFF")
    label("p2-h3", 1706, hy, 530, 32, "<b>DURABLE EFFECT</b>", fontsize=11,
          color="#FFFFFF")

    y = hy + 42
    for pid, ptitle, pfill, pstroke, steps in PHASES:
        rect(f"p2-ph{pid}", 40, y, 2210, 30,
             f"<b>{pid}</b> &nbsp; {ptitle}", fill=pfill, stroke=pstroke,
             font=pstroke, bold=0, fontsize=12, spacing=12)
        y += 38
        for n, actor, action, effect in steps:
            h = 104 if n in (18, 22) else 86
            rect(f"p2-r{n}", 40, y, 2210, h, "", fill="#FFFFFF",
                 stroke="#D5DBE3")
            badge(f"p2-b{n}", 54, y + 10, n)
            label(f"p2-a{n}", 108, y + 8, 250, 40, f"<b>{actor}</b>",
                  fontsize=11)
            label(f"p2-t{n}", 372, y + 6, 1320, h - 12, action, fontsize=10)
            label(f"p2-e{n}", 1706, y + 6, 530, h - 12, effect, fontsize=9,
                  color="#4C7A0B")
            y += h + 8
        y += 6

    rect("p2-note", 40, y + 4, 2210, 92,
         "<b>Two honest notes about this pipeline as it stands at v0.4.1.</b><br>"
         "&#9679; <b>ASR pass B is never invoked.</b> <code>TranscriptionService.confirm()</code> "
         "(wide beam, word timestamps, keyword-primed prompt) exists and is unit-tested, but no worker "
         "calls it. So <code>fuzzy</code>, <code>phonetic</code> and <code>semantic</code> matches are "
         "flagged <code>requires_confirmation</code> and then persisted <i>without</i> a confirming "
         "second decode. No transcript row is ever written with <code>asr_pass='b'</code>.<br>"
         "&#9679; <b>The S3 publish backlog is not a recovery path yet.</b> "
         "<code>_publish_backlog</code> selects mentions with a NULL <code>result_s3_key</code> and only "
         "logs the count &mdash; it never re-publishes them. Evidence capture (step 21) <i>is</i> "
         "retried properly by its own idle sweep.",
         fill="#FFF6F6", stroke=SGRED, fontsize=10)
    return "\n".join(cells)


# =========================================================================== #
# PAGE 3 - CI/CD
# =========================================================================== #
DSTEPS = [
    ("D1", "GitHub Actions",
     "A commit lands on <code>main</code> &mdash; the only deployment source. No staging branch, no "
     "release branch, no tag can trigger a deployment. <b>CI</b> runs ruff, pytest on 3.11 and 3.12, "
     "and bandit + pip-audit; <b>CodeQL</b> analyses Python in parallel.",
     "5 protected checks must exist:<br>Lint (ruff) &middot; Tests 3.11 &middot; Tests 3.12<br>"
     "Security (bandit + pip-audit) &middot; Analyze Python"),
    ("D2", "deploy-main.yml",
     "Triggered by <code>workflow_run</code> on CI completion (branch main), <b>not</b> by push &mdash; "
     "so a commit that failed CI can never deploy. Gates, in order: "
     "<code>AUTO_DEPLOY_ENABLED == \"1\"</code>, conclusion success, event push, head_branch main, "
     "and the SHA must match <code>^[0-9a-f]{40}$</code>. Then it polls the check-runs API for up to "
     "30 minutes until <b>all five checks are green on that exact SHA</b>.",
     "concurrency radio-production-main-deploy<br>cancel-in-progress: false<br>"
     "environment: production"),
    ("D3", "OIDC &rarr; STS",
     "<code>aws-actions/configure-aws-credentials</code>, pinned to a commit SHA, exchanges the GitHub "
     "OIDC token for short-lived STS credentials. The trust policy allows "
     "<code>sts:AssumeRoleWithWebIdentity</code> only when <code>aud = sts.amazonaws.com</code> and "
     "<code>sub = repo:&lt;owner&gt;/&lt;repo&gt;:environment:production</code>. The workflow then asserts the "
     "returned ARN and account id are the expected ones.",
     "GitHubActionsRadioDeployRole<br>role-duration 7200 s<br>"
     "<b>id-token: write &mdash; no secrets.* anywhere</b>"),
    ("D4", "SSM SendCommand",
     "Verifies the instance is <code>Online</code>, then verifies the pinned document version is numeric "
     "and matches <code>describe-document</code> exactly &mdash; <code>$LATEST</code> and "
     "<code>$DEFAULT</code> are explicitly forbidden. Sends <b>one</b> parameter: the 40-hex commit. "
     "The role can call <code>ssm:SendCommand</code> on only two documents and is explicitly "
     "<b>denied</b> AWS-RunShellScript, StartSession, EC2 Instance Connect, iam:*, sts:AssumeRole, "
     "ec2:RunInstances, cloudformation:* and secretsmanager:*.",
     "RadioBroadcastDeployMain<br>--document-version &lt;numeric&gt;<br>"
     "--max-concurrency 1 --max-errors 0"),
    ("D5", "SSM Agent on the host (root)",
     "Reads the commit from the environment (<code>ENV_VAR</code> interpolation, re-validated as 40 "
     "lower-case hex), requires <code>uname -m = aarch64</code> and <code>/var/lib/radio</code> to be a "
     "real mount point. Then, as <code>ec2-user</code>: <code>git fetch</code> only &mdash; never pull, "
     "reset or checkout &mdash; asserts <code>merge-base --is-ancestor &lt;sha&gt; origin/main</code>, and "
     "<code>git archive</code>s the commit to a temp dir before running its own "
     "<code>scripts/main-auto-deploy.sh</code>.",
     "/var/lib/radio/logs/deployments/<br>deploy-&lt;sha&gt;-&lt;stamp&gt;.log (0600 root)<br>"
     "flock /var/lock/radio-main-auto-deploy.lock"),
    ("D6", "main-auto-deploy.sh &rarr; deploy-compose.sh",
     "Nine stages: validate repo &rarr; lock &rarr; validate host (free space) &rarr; host prerequisites "
     "(pinned Docker toolchain by SHA-256) &rarr; runtime directories &rarr; production config (created "
     "<b>once</b>, never regenerating the token secret) &rarr; first-install vs normal-update &rarr; "
     "models (verify-first; a model that fails verification is never overwritten &mdash; the deploy dies) "
     "&rarr; deploy the stage. First install runs <b>api &rarr; core &rarr; full</b> on the same commit, "
     "each fully verified before the next.",
     "stages: api &rarr; core &rarr; full<br>"
     "release id = <b>commit + stage</b><br>"
     "/var/lib/radio/releases/&lt;sha&gt;/&lt;stage&gt;"),
]

GATES = [
    "1 &nbsp;tooling + sha256 present",
    "2 &nbsp;source repo readable, commit present, tree clean",
    "3 &nbsp;flock /var/lock/radio-compose-deploy.lock",
    "4 &nbsp;/var/lib/radio is a real mount point",
    "5 &nbsp;source compose.env",
    "6 &nbsp;resolve the host radio uid:gid",
    "7 &nbsp;directory ownership is writable",
    "8 &nbsp;env files exist, mode 600/640/400/440, no placeholder<br>"
    "&nbsp;&nbsp;&nbsp;secret, <b>no static AWS credential</b>",
    "9 &nbsp;validate the publish host",
    "10 free space: / 3072 MiB, /var/lib/radio 2048 MiB",
    "11 immutable release via git archive + manifest + secret-scan",
    "12 <code>docker compose config</code> resolves",
    "13 verify-models.py against models.lock.json (full stage)",
    "14 build images tagged <b>:&lt;sha&gt;</b> &mdash; never pulled",
    "14a validate config <b>inside the built image</b><br>"
    "&nbsp;&nbsp;&nbsp;&nbsp;(--network none, --read-only, --cap-drop ALL)",
    "15 backup SQLite (.backup, integrity_check, S3) then migrate<br>"
    "&nbsp;&nbsp;&nbsp;&nbsp;in a one-shot --network none container",
    "16 <code>compose up -d --no-build --pull never</code>, wait 300 s for<br>"
    "&nbsp;&nbsp;&nbsp;&nbsp;health, reconcile out-of-stage services",
    "&#10003; smoke-test.sh against http://127.0.0.1:8788",
    "&#10003; only now: flip current / previous symlinks, write state.json",
]


def page3() -> str:
    cells.clear()
    _boxes.clear()

    label("p3-title", 40, 24, 1700, 34,
          "CI/CD &mdash; GitHub &rarr; OIDC &rarr; SSM &rarr; EC2", fontsize=26, bold=1)
    label("p3-sub", 40, 60, 2100, 44,
          "<b>main is the only deployment source.</b> A reviewed change lands on main, every "
          "protected check passes on that exact commit, and the commit deploys itself.<br>"
          "There is no SSH, no port 22, no EC2 key pair, no static AWS credential, and no input "
          "through which an arbitrary command can be sent.", fontsize=12)

    y = 124
    for i, (did, actor, action, effect) in enumerate(DSTEPS):
        h = 120
        rect(f"p3-r{i}", 40, y, 2210, h, "", fill="#FFFFFF", stroke="#D5DBE3")
        badge(f"p3-b{i}", 54, y + 12, did, fill=DEPLOY_BADGE, d=34, fontsize=11)
        label(f"p3-a{i}", 100, y + 10, 250, 40, f"<b>{actor}</b>", fontsize=12)
        label(f"p3-t{i}", 364, y + 8, 1320, h - 16, action, fontsize=10)
        label(f"p3-e{i}", 1698, y + 8, 540, h - 16, effect, fontsize=9,
              color=DEPLOY_BADGE)
        y += h + 10

    y += 12
    rect("p3-gates", 40, y, 1090, 620,
         "deploy-compose.sh &mdash; 16 pre-flight gates, in order",
         fill="#F5F5FE", stroke=DEPLOY_BADGE, font=DEPLOY_BADGE, bold=1,
         fontsize=13)
    label("p3-gatesn", 58, y + 30, 1050, 30,
          "Gates 1&ndash;13 leave the running release <b>completely untouched</b>. "
          "Nothing is built, pulled or started until every one has passed.",
          fontsize=9, color=GREY)
    gy = y + 64
    for i, g in enumerate(GATES):
        gh = 38 if "<br>" in g else 22
        rect(f"p3-g{i}", 58, gy, 1050, gh, g, fill="#FFFFFF",
             stroke="#DDDDF0", fontsize=9, spacing=6)
        gy += gh + 2

    rect("p3-rb", 1160, y, 1090, 240,
         "Rollback &mdash; two mechanisms, one contract",
         fill="#FFF6F6", stroke=SGRED, font=SGRED, bold=1, fontsize=13)
    rect("p3-rb1", 1178, y + 34, 1054, 62,
         "<b>Automatic</b> &mdash; <code>restore_previous_release</code> inside "
         "deploy-compose.sh. Runs only after containers were touched and only when a previous "
         "commit+stage exists. It deliberately does not call the operator script, which would "
         "deadlock on the lock this process already holds.",
         fill="#FFFFFF", stroke="#E8C4C4", fontsize=9)
    rect("p3-rb2", 1178, y + 102, 1054, 50,
         "<b>Manual</b> &mdash; <code>scripts/rollback-compose.sh --previous</code> or "
         "<code>--to-commit &lt;sha&gt;</code>. Restores existing artifacts by moving a symlink.",
         fill="#FFFFFF", stroke="#E8C4C4", fontsize=9)
    rect("p3-rb3", 1178, y + 158, 1054, 68,
         "<b>Both restore code and images only. Neither ever restores the database.</b> "
         "Reverting a SQLite file would discard every mention written since the backup, and the "
         "schema is forward-only by policy (ADR-004). A backup <i>is</i> taken first; state records "
         "<code>\"migration\": \"not-run (rollback)\"</code> and <code>\"database_restored\": false</code>.",
         fill="#FFFFFF", stroke="#E8C4C4", fontsize=9)

    rect("p3-ev", 1160, y + 262, 1090, 238,
         "Evidence that there is no SSH and no static credential",
         fill="#F7F8FA", stroke=GREY, font=GREY, bold=1, fontsize=13)
    ev = [
        "IAM <code>Deny</code> &mdash; ssm:StartSession / ResumeSession / TerminateSession and both "
        "ec2-instance-connect SSH actions, on <code>*</code>",
        "IAM <code>Deny</code> &mdash; ssm:SendCommand against AWS-RunShellScript, "
        "AWS-RunPowerShellScript, AWS-RunRemoteScript",
        "The role carries <b>no s3: and no sqs: permission at all</b> &mdash; those belong to the EC2 "
        "instance role, and a test asserts it",
        "<code>permissions: id-token: write</code> and no <code>secrets.*</code> reference anywhere in "
        "the deploy workflows",
        "<code>reject_static_aws_credentials()</code> fails the deployment if an env file assigns "
        "AWS_ACCESS_KEY_ID / SECRET / SESSION_TOKEN",
        "The SSM document accepts exactly one parameter and no shell command",
    ]
    ey = y + 296
    for i, e in enumerate(ev):
        rect(f"p3-ev{i}", 1178, ey, 1054, 30, "&#9679; " + e, fill="#FFFFFF",
             stroke="#D5DBE3", fontsize=9, spacing=6)
        ey += 33

    return "\n".join(cells)


# =========================================================================== #
def build() -> str:
    pages = [
        ("arch", "1. AWS architecture", page1(), 2960, 2010),
        ("pipeline", "2. Backend pipeline (22 steps)", page2(), 2320, 2700),
        ("cicd", "3. CI/CD and deployment", page3(), 2320, 1660),
    ]
    out = ['<mxfile host="app.diagrams.net" agent="Radio Broadcast Analysis" '
           f'type="device" pages="{len(pages)}">']
    for pid, name, body, pw, ph in pages:
        out.append(f'  <diagram id="page-{pid}" name="{esc(name)}">')
        out.append(
            '    <mxGraphModel dx="1600" dy="900" grid="1" gridSize="10" '
            'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
            f'pageScale="1" pageWidth="{pw}" pageHeight="{ph}" math="0" '
            'shadow="0" background="#FFFFFF">')
        out.append('      <root>')
        out.append('        <mxCell id="0" />')
        out.append('        <mxCell id="1" parent="0" />')
        out.append(body)
        out.append('      </root>')
        out.append('    </mxGraphModel>')
        out.append('  </diagram>')
    out.append('</mxfile>')
    return "\n".join(out)


if __name__ == "__main__":
    xml = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(xml, encoding="utf-8")
    print(f"wrote {OUT}  ({len(xml):,} bytes)")
