"""Optional native painting window based on Tk.

The browser painting window (``python -m layerpaint.server``) is the
zero-dependency graphical entry point and works everywhere.  This Tk front
end is provided for environments where CPython ships Tkinter; it drives the
same :class:`~layerpaint.server.PaintingSession` evaluator through direct
method calls rather than HTTP.  When Tkinter is unavailable the module raises
a clear error telling the user to open the browser window instead.
"""

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:  # pragma: no cover - environment dependent
    tk = None


def launch(session):
    if tk is None:
        raise RuntimeError(
            "Tkinter is not available in this Python installation. "
            "Use the browser painting window instead: "
            "python -m layerpaint.server")

    root = tk.Tk()
    root.title("图层合成绘画窗口 (Tk)")
    project = session.project

    state = {"selected": project.root, "backend": session.backend_name}

    tree = ttk.Treeview(root)
    tree.pack(side="left", fill="y")

    def refresh_tree():
        tree.delete(*tree.get_children())

        def add(node, parent=""):
            label = "%s [%s%s]" % (
                node.name,
                "group" if node.kind == "group" else "layer",
                ", pass-through" if node.kind == "group" and not node.isolated
                else "",
            )
            iid = tree.insert(parent, "end", text=label, values=(node.uid,))
            for child in getattr(node, "children", []):
                add(child, iid)

        add(project.root)

    def on_select(_event):
        sel = tree.focus()
        if not sel:
            return
        uid = int(tree.item(sel)["values"][0])
        state["selected"] = project.get(uid)
        sync_controls()

    tree.bind("<<TreeviewSelect>>", on_select)

    controls = ttk.Frame(root, padding=8)
    controls.pack(side="left", fill="y")

    ttk.Label(controls, text="混合模式").pack(anchor="w")
    from ..blend import BLEND_MODES
    mode_var = tk.StringVar()
    mode_box = ttk.Combobox(controls, textvariable=mode_var,
                            values=sorted(BLEND_MODES), state="readonly")
    mode_box.pack(anchor="w", fill="x")

    ttk.Label(controls, text="不透明度").pack(anchor="w")
    opacity = tk.DoubleVar(value=1.0)
    ttk.Scale(controls, variable=opacity, from_=0.0, to=1.0,
              orient="horizontal").pack(anchor="w", fill="x")

    iso_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(controls, text="隔离组（关闭=穿透）",
                    variable=iso_var).pack(anchor="w", pady=4)
    clip_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(controls, text="裁切到下层",
                    variable=clip_var).pack(anchor="w")

    def sync_controls():
        node = state["selected"]
        mode_var.set(node.blend_mode)
        opacity.set(node.opacity)
        clip_var.set(node.clipped)
        if node.kind == "group":
            iso_var.set(node.isolated)

    def apply_changes():
        node = state["selected"]
        try:
            node.blend_mode = mode_var.get()
            node.opacity = float(opacity.get())
            node.clipped = bool(clip_var.get())
            if node.kind == "group":
                node.isolated = bool(iso_var.get())
            project.validate()
            session.evaluator.touch_node(node.uid)
            raster, _ = session.evaluator.evaluate()
            info["text"] = "求值完成：%d×%d，后端 %s" % (
                raster.width, raster.height, state["backend"])
        except Exception as exc:  # surface compositing errors in the UI
            messagebox.showerror("图层合成错误", str(exc))

    ttk.Button(controls, text="应用并求值", command=apply_changes).pack(
        anchor="w", pady=8, fill="x")
    info = ttk.Label(controls, text="选择一个图层开始", wraplength=200)
    info.pack(anchor="w")

    refresh_tree()
    root.mainloop()


def main(argv=None):
    import argparse
    from ..server import PaintingSession
    from ..demo import build_demo_project
    parser = argparse.ArgumentParser(description="Native Tk painting window")
    parser.add_argument("--backend", choices=["ref", "acc"], default="ref")
    args = parser.parse_args(argv)
    launch(PaintingSession(build_demo_project(), backend=args.backend))


if __name__ == "__main__":
    main()
