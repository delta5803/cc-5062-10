# 图层合成求值核心 — 架构

## 1. 图形入口（主要使用方式）

本项目交付两个图形入口，CLI 仅作为辅助：

| 入口 | 启动方式 | 依赖 | 说明 |
| --- | --- | --- | --- |
| 浏览器绘画窗口（默认） | `./run_paint.sh` 或 `PYTHONPATH=src python3 -m layerpaint.server` | 仅 Python 标准库 | 内置 `http.server`，浏览器只负责显示与输入，**每个合成像素都由 Python 求值核心计算** |
| Tk 原生窗口（可选） | `PYTHONPATH=src python3 -m layerpaint.backends.tkapp` | 需要安装了 Tkinter 的 Python | 直接调用同一 `PaintingSession`/`Evaluator`；无 Tkinter 时给出明确错误并提示改用浏览器窗口 |

绘画窗口能力：

- 图层/组树：新增、删除、重排、重命名，嵌套组与空组可见；
- 选中节点可调整不透明度、切换 16 种混合模式、切换隔离/穿透、切换“裁切到下层”；
- 真实软笔绘画：可调颜色、不透明度、半径、硬度，支持半透明叠加笔迹；可在图层或共享蒙版上绘画；
- 结果区显示合成 PNG（实际求值输出）；点击任意像素，右侧给出**逐层 RGBA（字节与归一化值）颜色求值**；
- 重算日志区显示缓存命中、矩形重算、蒙版应用、穿透组传播等依赖事件；
- 异常（如穿透组带非 1 不透明度、裁切无基底）由界面错误条提示，不静默吞掉；
- 可保存自包含 JSON 工程、导出扁平化 PNG，并可在 CPU 参考 / 加速后端间切换对比。

## 2. 模块划分

```
src/layerpaint/
  colorspace.py    颜色空间、sRGB 线性转换、预乘/非预乘、零 alpha 不变量
  blend.py         完整 W3C 16 混合模式（11 可分离 + 4 不可分离 + normal）
  raster.py        直乘 RGBA8 光栅、软笔笔迹、零依赖 PNG 读写
  model.py         Layer / Group / Project 数据模型与结构校验
  engine.py        Evaluator：求值、裁切链、隔离/穿透、缓存与脏区依赖
  backends/
    reference.py   CPU 参考后端：逐像素浮点，严格按 W3C 公式
    accelerated.py 加速后端：紧凑浮点循环 + 一次性解析混合函数
    tkapp.py       可选 Tk 图形入口
  serialization.py 自包含 JSON 工程（base64 PNG 内嵌光栅与蒙版）
  demo.py          覆盖全部特性的示例工程
  cli.py           命令行辅助（render / demo / pixel / compare）
  server.py        浏览器绘画窗口的本地 HTTP 服务与 API
web/index.html     绘画窗口前端（零依赖原生 HTML/JS）
```

## 3. 颜色空间与 alpha 表示（明确约定）

- 存储：sRGB 编码、**非预乘（straight / non-premultiplied）RGBA8**。
- 运算：统一归一化到 `[0,1]` 浮点，仅在输出边界量化回字节。
- 混合：按 W3C Compositing and Blending Level 1，在 **gamma 编码的 sRGB 值**上定义；
  `srgb_to_linear` / `linear_to_srgb` 显式提供，不做隐式转换。
- 预乘只在合成公式内部显式发生：

  ```
  co_p = B(cb, cs) * sa                 # 混合后预乘
  ao   = sa + da*(1-sa)
  ro_p = co_p + cb_p*(1-sa)             # 预乘空间合成
  ro   = unpremultiply(ro_p, ao)        # 回到非预乘
  ```

- **透明像素隐藏颜色不污染边缘**：alpha 为 0 的像素存储恒为 `(0,0,0,0)`。
  `unpremultiply` 对零 alpha 一律返回黑色；`strip_hidden_color`、蒙版、裁切
  各路径都维护该不变量。因此透明像素里的隐藏颜色无法经任何运算渗入软边。

## 4. 组：隔离 vs 穿透（按定义执行，不先摊平）

- **隔离组**：子图层先在透明黑背景上合成得到组自身结果，再以组的混合模式/不透明度
  整体进入父级背景一次。
- **穿透组（pass-through）**：组没有自身背景，子图层直接读父级的实时背景。
- W3C 堆叠规则：不透明度 `<1` 或非 normal 混合会强制隔离。因此
  穿透组必须 `opacity=1 且 blend=normal`，否则校验报错。
- **组不透明度 ≠ 每层不透明度**：组不透明度作用于已隔离的组整体结果；
  测试 `test_group_opacity_not_equal_to_per_layer_opacity` 用手算案例证明二者不同，
  核心不会把图层先简单摊平再“猜”组效果。

## 5. 裁切链与链中断

- 裁切节点（图层或组）受其下方最近的**非裁切同级节点**（裁切基底）自身 alpha 限制。
- 裁切在预乘空间做线性插值：

  ```
  result = base_alpha * node_render + (1 - base_alpha) * pre_base_backdrop
  ```

  其中 `pre_base_backdrop` 是基底节点放置**之前**的背景快照，保证半透明基底也正确。
- 任一非裁切同级节点都会成为新基底，即**裁切链中断**（见 `test_chain_breaks_at_non_clipped_sibling`）。
- 裁切节点不能是组内第一个子节点（无基底），校验报错。

## 6. 缓存边界与依赖驱动的重算范围

`Evaluator` 为每个图层与隔离组缓存“纯内容”局部渲染；蒙版、不透明度、混合模式、
裁切都属于**放置阶段**，不进入内容缓存键。

- 节点编辑：版本号 +1，脏区沿祖先传播；不相关分支（如隔离组里的改动）不会触发穿透组重算。
- 蒙版编辑（隔离组与穿透组可共享同一蒙版）：
  - **隔离组读者**：蒙版在它被放置进父级时应用，其自包含内容缓存保持有效，
    仅在蒙版脏矩形上重新 `mask-apply`；
  - **穿透组读者**：它没有自身缓存，蒙版效果被烘焙进**最近的隔离祖先**的局部
    渲染，因此该祖先的内容缓存按脏矩形失效并在透明画布上重建该矩形后拼回；
    子矩形补丁绝不在旧光栅上重复合成。
- `reevaluate_dirty_rect(rect)` 在透明画布上对整棵树只重算该矩形，再拼回根光栅，
  使两种缓存边界在 `log` 中可核查（见 `test_shared_mask_rect_distinguishes_isolated_and_passthrough`
  与 `test_passthrough_mask.py`）。重算结果与全量求值逐字节一致由专门测试锁定。

## 7. 两个后端如何互验

- `ReferenceBackend`：逐像素浮点，W3C 定义公式，是“真值”。
- `AcceleratedBackend`：每像素只做一次混合函数解析、紧凑浮点循环，非可分离模式回退参考数学。
- 全部 16 模式、随机半透明像素、软笔笔迹光栅、蒙版、裁切均有双后端对比测试，
  差异不超过 1 字节量化容差；demo 工程在两后端下输出完全一致。

## 8. 测试与可核查性

`python3 -m unittest discover -s tests` 覆盖：手算小尺寸案例、半透明软笔迹、
透明边缘不污染、嵌套组、空组、裁切链中断、共享蒙版缓存边界、序列化往返、
PNG 往返、后端一致性、HTTP API 端到端。
