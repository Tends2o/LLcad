# Recipes (handheld devices)

All snippets assume `from llcad import Build, Frame, rrect_pts, read_csv` and `b = Build(...)`.
Coordinates: device axis +Z (handle bottom at z=0), front (controls) −Y, right +X.
Every recipe ends with what to check in the 4-view look or with a measurement.

## 1 Grip loft from CSV stations

Contour tables come from the Reference Pack (scripted extraction, CCW width-per-height tables).
```python
# refs/grip.csv: z,w,d,r   (mm; w along X, d along Y, r corner radius)
grip = b.grip_loft_csv("p01-grip", "refs/grip.csv", z="z", w="w", d="d", r_col="r", segs=8, part="pt-grip")
```
Same `segs` at every station gives equal segment counts (the loft stays controlled). For an
off-centre section use `cx`/`cy` columns. Check: station widths at 3 heights with cad_measure
(`all_pairs`/bounds) against the table (≤0.2 mm on orthographic sources).

## 2 Tilted head frame

```python
H = b.tilted_head_frame("fr-head", pivot=(0, 0, 152.0), tilt_deg=-14, axis=(1, 0, 0))
barrel = b.ring("p10-barrel", 27.5, 16.4, 25, z=-12.5, part="pt-head", frame="fr-head")
lens, seat = b.lens("p11-lens", 16.1, 0.6, 125, z0=10.8, part="pt-lens", frame="fr-head",
                    seat={"wall": 0.9, "lip": 0.6, "height": 1.6, "part": "pt-seat"})
```
Features with `frame="fr-head"` are built in head coordinates (head axis = local +Z); the part must
live in a frame that contains fr-head (world does). Zero placement features, unlike rotate+transform
chains per feature (357 of 839 features of one earlier model were such placements). `H` (a Python Frame)
places single solids: `at=H.then(Frame((0, 0, 5)))`.

## 3 Knurled / grooved ring

```python
knurl = b.knurl_ring("p12-focus", r_out=27.3, r_in=16.4, h=14, count=72, z=12, part="pt-focus", frame="fr-head")
```
One solid (ring minus a circular pattern of axial grooves): grooves cannot float off the ring.
Check: groove count and pitch in a close-up; ring does not intersect the head shell (contact graph).

## 4 Ring text (text on an arc)

```python
F = Frame((0, 0, 30.2))                              # ring face plane, text around its Z axis
t = b.text("p13-ring-text", "LLcad Precision Optics 10x", 1.2, 0.08,
           arc={"radius": 25.0, "center_angle_deg": 90, "side": "outside"}, at=F, part="pt-print")
```
Native `text` with `arc` when the server has it; else per-glyph outlines placed on the arc
(one affine per glyph; keep one ring text per patch on old servers: CPU limit). Outside text reads
left→right at the top; `side:"inside"` reads left→right at the bottom. Relief ≥0.05 mm, colour part
of its own. Check: reading direction and centre angle in the reference view.

## 5 SVG logo placement (original vectors, measured colour)

```python
from llcad import svg_colours
print(svg_colours("refs/logo.svg"))                 # e.g. ['#1f6fd1', '#ffffff']
Fl = Frame.plane("back", (0, 10.25, 35.0))           # face normal +Y, logo upright
tri = b.svg_emblem("p14-logo-mark", "refs/logo.svg", 0.08, width=9.0, at=Fl,
                   element_filter={"fill": "#1f6fd1"}, part="pt-logo-blue")
txt = b.svg_emblem("p14-logo-txt", "refs/logo.svg", 0.1, width=9.0, at=Fl,
                   element_filter={"fill": "#ffffff"}, part="pt-logo-white")
b.part("pt-logo-blue", "Logo mark", colour="#2a6cc0", material="Print matte", outputs=[tri])
```
Scale comes from the whole emblem, so filtered colour layers keep their relative placement. Use the
device colour measured on product photos (a style-guide brand colour and the colour printed on the device often differ by ΔE00 > 10).
Stagger layers (0.08 / 0.1 mm) so no coplanar faces of different colour touch. Never redraw a
brand mark from memory. Check: centroid (mm) and size ratio 0.9–1.1 against the registered view.

## 6 Raised button whose cap stays visible

```python
K = Frame.plane("front", (0, -9.55, 124.8))           # housing surface, normal −Y (outward)
key = b.control_key("p20-key-p", w=7.0, h=9.4, r=1.1, travel=1.5, proud=0.3, seam=0.15, at=K,
                    part="pt-key", housing_part="pt-body")
body = b.cut("p01-housing", grip, key["recess"], part="pt-body")
```
The cap top is `proud` (≥0.2 mm) above the surface, the recess is `seam` wider, so the cap is never
buried in the housing (keys of one earlier model were 8 mm off and covered). Check: visible fraction of
the key part ≥80 % in the front view; no coincident faces with the housing.

## 7 LED / status window

```python
W = Frame.plane("front", (0, -9.25, 127.5))
led = b.led_window("p21-led", 1.9, 1.9, 0.9, depth=0.35, at=W, part="pt-led-green", housing_part="pt-body")
body = b.cut("p01-housing-2", body, led["cutter"], part="pt-body")
b.part("pt-led-green", "LED green", colour="#5bbd41", material="LED", outputs=[led["window"]])
row = b.indicator_row("p22-charge", 3, 3.6, 0.95, 0.35, at=W.moved(0, 3, 0), parts=["pt-l1", "pt-l2", "pt-l3"])
```
Window flush with the surface in its own coloured part (emissive/glass material class); the cutter is
exactly the window outline. Check: ΔE in the albedo channel, count of LEDs.

## 8 Lens seated on its seat

```python
lens, seat = b.lens("p30-lens", r=16.1, t_edge=0.6, R=125, z0=10.8, face="+z", frame="fr-head",
                    part="pt-lens", seat={"wall": 0.9, "lip": 0.6, "height": 1.6, "part": "pt-seat"})
```
The flat face of the lens lies exactly on the seat's top face (declared contact, hidden), the lip
holds it, glass is its own part (transmission material). Check: contact distance ≤0.02 mm, no
floating lens (contact graph), sightline along the optical axis unobstructed.

## 9 Shell with a seam

```python
outer = b.grip_loft_csv("p40-shell-outer", "refs/grip.csv", part="pt-shell")
shell = b.shell("p40-shell", outer, 1.2, opening="top", part="pt-shell")
seam = b.ring("p41-seam", 23.0, 21.9, 0.25, z=95.0, part="pt-shell")      # 0.25 mm wide groove band
shell = b.cut("p40-shell-seam", shell, seam, part="pt-shell")
```
`shell` opens exactly one planar top/bottom face, so shell the simple lofted body first and add
details afterwards. A visible seam is a groove ≥0.1 mm wide and deep (thinner is invisible and
flickers). For two half-shells: `split_by_plane` (new builds) or intersect with two boxes.

## 10 Colour naming

Part names `Material #RRGGBB · Part`, e.g. `Plastic satin #dddddb · Grip`,
`Print matt #4d4c49 · Lettering`, `Glass #e1e7ee · Window`, `LED #5bbd41 · Charge indicator`.
`b.part(pid, name, colour="#dddddb", material="Plastic satin")` builds it; the viewer parses
the hex. One colour per part: a two-colour logo is two parts.

## 11 USB-C receptacle

```python
U = Frame.plane("bottom", (0, 0, 0.0))                 # face normal −Z at the handle heel
usb = b.usb_c("p50-usb", at=U, depth=7.0, part="pt-body", liner_part="pt-usb")
body = b.cut("p01-housing-3", body, usb["cutter"], part="pt-body")
```
Opening 8.94 × 3.26 mm with full radii plus the tongue; the cutter reaches 0.5 mm outside the
surface so the cut is never tangent.

## 12 Whole model from one generator (MVP)

One `.llcad/gen/mvp.py` builds frames, envelope, every part slot with its colour and all major
shapes; `python3 .llcad/gen/mvp.py --model <id> --http --gates` (or the printed patch_ref line).
Details are separate generators per region (`gen/p3-head.py`, `gen/p3-controls.py`, `gen/p3-marks.py`)
that build on the head revision; parts keep their ids, outputs are merged by the DSL.
