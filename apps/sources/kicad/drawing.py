"""Drawing a KiCad file into what the preview pages show. No Django here, so the background worker can use it too."""
from apps.design.gerber import Budget

from . import pcb, schematic


def draw(text, kind, highlight=None, prefix="k", instance=None, budget=None, model=None):
    """{"ok": True, "kind": …, "svg": …, facts…} for a schematic ("sch") or PCB ("pcb"). Raises KicadError or GerberLimitError.
    `model` is an already parsed Schematic or Board; `instance` is (sheet path, KiCad 6 list) for one copy of a sheet."""
    budget = budget or Budget()  # one time allowance for reading and drawing
    if kind == "sch":
        sch = model or schematic.load_schematic(text, budget)
        if instance:
            sch.use_instance(*instance)
        return {"ok": True, "kind": "sch", "svg": schematic.draw_schematic(sch, highlight=highlight, budget=budget),
                "parts": sch.parts(), "sheets": [{"name": s.name, "file": s.file, "uuid": s.uuid} for s in sch.sheets],
                "nets": sorted(sch.nets()), "title": sch.title, "newer": sch.newer, "version": sch.version,
                "uuid": sch.uuid, "instance_paths": sch.instance_paths(),
                "symbol_instances": {k: list(v) for k, v in sch.symbol_instances.items()}}
    board = model or pcb.load_pcb(text, budget)
    result = pcb.render_pcb("", board=board, highlight=highlight, prefix=prefix, budget=budget)
    result.pop("thumb_top", None)  # the Gerber viewer's thumbnail; previews don't use it
    result.update({"ok": True, "kind": "pcb", "newer": board.newer, "version": board.version})
    return result
