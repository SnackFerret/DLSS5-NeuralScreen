r"""The stabilizer does not leave a moving face's shadows behind it (#160).

The stabilizer (native/stabilizer.inl) blends last frame's edit, carried
along the motion, into this frame's where a per-pixel test trusts it. On soft
content - a face whose shadow side is a slow gradient, over a smooth
background - that test sees almost no difference between "the shadow moved"
and "it did not", so the old shadow stayed where the face had been and faded
over several frames; at 30 fps that is a trail of a tenth of a second, which
is what the reporter saw from 2.2.0 on. A change in the edit larger than the
network's own shimmer now passes through, and the history's weight follows
the time between frames instead of the frame count.

Checked on the real worker in Boost with known motion (tests/nr_motion_harness.py,
the "soft" scene, 30 fps pinned with NS_STAB_FRAME_MS), stabilizer on against
NS_STAB=0 on the same frames: the edit left on the background the face has
just uncovered differs between the two by little more than the shimmer far
away does - with exact vectors and with NVOFA-like coarse ones.

Run:  runtime\python.exe tests\test_nr_stabilizer_trail.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import nr_motion_harness as hz  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

FRAMES = 32
FRAME_MS = "33.3"     # the reporter's game ran at 30 fps
# What the stabilizer may leave behind beyond the shimmer it removes, as a
# share of the edit on the face.
GHOST_SHARE = 0.08


def _outputs(scene, variant: str, stab: str) -> list:
    run = hz.run_worker(scene, variant, FRAMES,
                        env={"NS_STAB": stab, "NS_STAB_FRAME_MS": FRAME_MS})
    if not run.ok:
        raise RuntimeError(f"soft/{variant} NS_STAB={stab}: {run.error}")
    return run.outputs


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    failures = []
    for variant in ("gt", "coarse"):
        scene = hz.make_scene("soft", FRAMES, seed=1)
        off = _outputs(scene, variant, "0")
        on = _outputs(scene, variant, "1")
        m = hz.trail_metrics(scene, on, off)
        excess = m["ghost"] - m["floor"]
        print(f"    soft/{variant:7s} left behind {m['ghost']:.3f}, shimmer floor "
              f"{m['floor']:.3f}, edit on the face {m['edit_obj']:.2f} "
              f"(excess {100 * excess / max(1e-6, m['edit_obj']):.1f}% of it)")
        if m["edit_obj"] < 0.5:
            failures.append(f"soft/{variant}: the network made no edit on the face "
                            f"({m['edit_obj']:.2f}) - the scene measures nothing")
        elif excess > GHOST_SHARE * m["edit_obj"]:
            failures.append(f"soft/{variant}: the stabilizer leaves the face's edit "
                            f"behind ({m['ghost']:.3f} against a floor of "
                            f"{m['floor']:.3f}, edit {m['edit_obj']:.2f})")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a moving soft face leaves no trail of its edit behind it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
