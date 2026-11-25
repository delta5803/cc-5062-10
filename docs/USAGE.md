# 使用说明

## 图形入口（推荐）

### 浏览器绘画窗口（零依赖，任何环境可用）

```bash
./run_paint.sh            # 默认端口 8765
# 或
PYTHONPATH=src python3 -m layerpaint.server --port 8765
```

打开 `http://127.0.0.1:8765/`：

1. 左侧图层树选择图层或组；用“+ 图层 / + 组 / 删除”编辑结构。
2. 选中节点后调整不透明度、混合模式（16 种）、隔离/穿透、裁切到下层。
3. 选择画笔颜色、不透明度、半径、硬度，在画布上拖动即可画出软边半透明笔迹；
   “绘画目标”可切到蒙版，在共享蒙版上绘画。
4. 点击“求值合成”（或结束一笔自动求值）查看结果；点击任意像素查看逐层颜色求值。
5. 顶部可在 **CPU 参考 / 加速** 后端间切换，对比输出；右侧显示依赖驱动的重算日志。
6. “保存工程”下载自包含 JSON；“导出 PNG”下载扁平化合成图。
7. 结构非法时（穿透组带不透明度、裁切无基底等）顶部错误条会显示具体原因。

### Tk 原生窗口（可选，需 Tkinter）

```bash
PYTHONPATH=src python3 -m layerpaint.backends.tkapp
```

若当前 Python 未安装 Tkinter，会明确提示改用浏览器窗口。

## 命令行（仅辅助合成）

```bash
export PYTHONPATH=src

# 渲染内置示例工程为 PNG
python3 -m layerpaint.cli demo out.png --size 64
python3 -m layerpaint.cli demo out-acc.png --backend acc

# 渲染已保存工程（可只重算一个矩形）
python3 -m layerpaint.cli render project.lp.json out.png --backend ref
python3 -m layerpaint.cli render project.lp.json out.png --rect 8 8 24 24

# 查看某个像素逐层（底层到最终）的 RGBA 字节与归一化值
python3 -m layerpaint.cli pixel project.lp.json 16 16

# CPU 参考 vs 加速后端一致性
python3 -m layerpaint.cli compare project.lp.json
```

## 工程文件

`.lp.json` 为自包含 JSON：图层树、每节点不透明度/混合/隔离/裁切/蒙版引用，
以及 base64-PNG 内嵌的光栅与蒙版，可离线重新加载并得到相同合成结果。

## 跑测试

```bash
python3 -m unittest discover -s tests
```

## 示例产物

- `examples/composite.png`：示例工程的最终扁平化合成（含 177 种 alpha 值的软边）。
- `examples/isolated-group-local.png`：隔离组在透明背景上的局部渲染（中间层验真）。
- `examples/demo-project.lp.json`：可重新加载的完整工程。
