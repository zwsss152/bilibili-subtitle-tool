# -*- coding: utf-8 -*-
"""
苹果风格（macOS 浅色）tkinter 组件库
=====================================
为什么自绘：ttk 原生控件在 Windows 上由系统主题渲染，圆角、色彩、勾选符号
（clam 主题用 × 而不是 ✓）都无法按设计意图控制；按钮甚至会被渲染成「只有字没
有框」——因为在浅色卡片上把按钮底色也设成了白色。这里用 Canvas 自绘关键控件，
换取完全可控的视觉与交互反馈。

设计对齐 Apple HIG 浅色模式：
  - 底色 #F5F5F7，卡片纯白，发丝描边
  - 系统蓝 #007AFF 作唯一强调色，成功/失败用系统绿红
  - 圆角：按钮 8px，卡片 12px，输入井 8px
  - 字重只用 regular/500/bold 三档，靠字号与颜色拉层级
"""

import tkinter as tk
import tkinter.font as tkfont
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageTk


# ----------------------------------------------------------------------
# 设计令牌
# ----------------------------------------------------------------------
class T:
    # 表面
    bg            = "#F5F5F7"   # 窗口底
    surface       = "#FFFFFF"   # 卡片
    surface_alt   = "#F2F2F7"   # 次级按钮底 / 输入井
    fill_hover    = "#E9E9EE"
    fill_press    = "#DEDEE4"
    log_bg        = "#FBFBFD"

    # 描边
    border        = "#E5E5EA"   # 发丝线
    border_strong = "#D1D1D6"   # 输入框描边
    border_card   = "#DBDBE0"   # 卡片描边（用户要求边线可见，比发丝线深一档）
    border_input  = "#C9C9CE"   # 输入框描边（再深一档，边界明确）

    # 文字
    text          = "#1D1D1F"   # 主标签
    text2         = "#6E6E73"   # 次标签
    text3         = "#AEAEB2"   # 三级/占位

    # 系统色
    accent        = "#007AFF"
    accent_hover  = "#0A6FE0"
    accent_press  = "#0060C7"
    accent_tint   = "#E8F1FF"
    accent_tint2  = "#D8E9FF"
    success       = "#34C759"
    warning       = "#FF9F0A"
    error         = "#FF3B30"
    error_tint    = "#FFECEB"
    running       = "#007AFF"
    wait          = "#8E8E93"
    select        = "#E8F1FF"

    # 形状
    radius_button = 8
    radius_card   = 12
    radius_well   = 8

    # 间距（4pt 基准）
    s1, s2, s3, s4, s5, s6, s8 = 4, 8, 12, 16, 20, 24, 32


# ----------------------------------------------------------------------
# 基础设施
# ----------------------------------------------------------------------
def enable_dpi_awareness():
    """开启 Windows 高 DPI 感知，避免 Canvas 文字发虚（不生效则静默跳过）。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # Win8.1+
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()        # Win7 回退
    except Exception:
        pass


_FONT_CACHE = {}


def ui_font(root, size=10, weight="normal"):
    """挑一个当前系统上存在的、最接近 SF Pro 的字体。"""
    if (size, weight) in _FONT_CACHE:
        return _FONT_CACHE[(size, weight)]
    try:
        families = set(tkfont.families(root))
    except Exception:
        families = set()
    for name in ("Microsoft YaHei UI", "微软雅黑", "Segoe UI", "Arial"):
        if name in families:
            f = (name, size, weight)
            _FONT_CACHE[(size, weight)] = f
            return f
    f = ("TkDefaultFont", size, weight)
    _FONT_CACHE[(size, weight)] = f
    return f


def _bg_of(widget):
    """尽量读出父控件的底色，让 Canvas 圆角外沿与父级融为一体。"""
    for key in ("bg", "background"):
        try:
            return widget.cget(key)
        except Exception:
            continue
    return T.bg


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画一个圆角矩形（平滑样条）。

    ⚠️ 实测 Windows 上 1px 描边的底边会渲染成虚线（样条在直边上有亚像素抖动，
    跨像素抖动 = 视觉断续）。所有可见圆角一律改用下面的 RRect（弧+直线组合）。
    保留本函数仅为兼容外部引用。
    """
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [
        x1 + r, y1,  x2 - r, y1,  x2, y1,
        x2, y1 + r,  x2, y2 - r,  x2, y2,
        x2 - r, y2,  x1 + r, y2,  x1, y2,
        x1, y2 - r,  x1, y1 + r,  x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, splinesteps=28, **kw)


class RRect:
    """像素级圆角矩形：填充 = 2 矩形 + 4 扇形；描边 = 4 直线 + 4 圆弧。

    实测 smooth polygon 的 1px 描边在 Windows 上底边会渲染成虚线，
    这是用户看到「输入框底下是虚线」「按钮没有底色边缘发虚」的根因。
    本类渲染是实的。用法：
        shape = RRect(cv, x1, y1, x2, y2, r, fill="#fff", outline="#ddd")
        shape.set_bounds(...)   # 位置变化时重建
        shape.config(fill=..., outline=..., width=...)   # 换色
        shape.lower()           # 沉到其它 canvas item 之下
    """

    def __init__(self, cv, x1, y1, x2, y2, r, fill="", outline="", width=1):
        self.cv = cv
        self._tag = "rrect_%s" % (id(self),)   # 整组 items 用一个 tag 管理
        self._fill = fill or ""
        self._outline = outline or ""
        self._width = width
        self.set_bounds(x1, y1, x2, y2, r)

    def _build(self):
        cv = self.cv
        cv.delete(self._tag)         # tag 级删除：绝无漏删残留
        x1, y1, x2, y2, r = self._bounds
        r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        t = self._tag
        if self._fill:
            if r >= 1:
                cv.create_rectangle(x1 + r, y1, x2 - r, y2,
                                    fill=self._fill, width=0, tags=t)
                cv.create_rectangle(x1, y1 + r, x2, y2 - r,
                                    fill=self._fill, width=0, tags=t)
                # pieslice 必须显式 outline=fill 色：Tk 默认黑描边且 width=0 不生效
                for bx, by, start in ((x1, y1, 90), (x2 - 2 * r, y1, 0),
                                      (x1, y2 - 2 * r, 180), (x2 - 2 * r, y2 - 2 * r, 270)):
                    cv.create_arc(bx, by, bx + 2 * r, by + 2 * r, style="pieslice",
                                  start=start, extent=90,
                                  fill=self._fill, outline=self._fill,
                                  width=0, tags=t)
            else:
                cv.create_rectangle(x1, y1, x2, y2, fill=self._fill,
                                    width=0, tags=t)
        if self._outline:
            w = self._width
            if r >= 1:
                cv.create_line(x1 + r, y1, x2 - r, y1, fill=self._outline,
                               width=w, tags=t)
                cv.create_line(x2, y1 + r, x2, y2 - r, fill=self._outline,
                               width=w, tags=t)
                cv.create_line(x1 + r, y2, x2 - r, y2, fill=self._outline,
                               width=w, tags=t)
                cv.create_line(x1, y1 + r, x1, y2 - r, fill=self._outline,
                               width=w, tags=t)
                cv.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, style="arc",
                              start=90, extent=90, outline=self._outline,
                              width=w, tags=t)
                cv.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, style="arc",
                              start=0, extent=90, outline=self._outline,
                              width=w, tags=t)
                cv.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, style="arc",
                              start=270, extent=90, outline=self._outline,
                              width=w, tags=t)
                cv.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, style="arc",
                              start=180, extent=90, outline=self._outline,
                              width=w, tags=t)
            else:
                cv.create_rectangle(x1, y1, x2, y2, fill="",
                                    outline=self._outline, width=w, tags=t)
        # Rebuilding on hover/focus must not place the fill above its label.
        cv.tag_lower(self._tag)

    def set_bounds(self, x1, y1, x2, y2, r):
        self._bounds = (x1, y1, x2, y2, r)
        self._build()

    def config(self, fill=None, outline=None, width=None):
        changed = False
        if fill is not None and fill != self._fill:
            self._fill, changed = fill, True
        if outline is not None and outline != self._outline:
            self._outline, changed = outline, True
        if width is not None and width != self._width:
            self._width, changed = width, True
        if changed:
            self._build()

    def lower(self):
        # 整组一次性沉底，组内相对顺序保持（fill 在下、描边在上）
        self.cv.tag_lower(self._tag)


# ----------------------------------------------------------------------
# 圆角按钮
# ----------------------------------------------------------------------
class ButtonBackground(RRect):
    """用一张抗锯齿图像绘制按钮，避免 Tk 扇形拼接的凸点和接缝。

    文字仍由 Canvas 绘制；缓存最近的尺寸和交互状态，悬停不重复栅格化。
    图像引用与控件同寿命，背景始终位于文字下方。
    """

    def __init__(self, *args, **kwargs):
        self._images = OrderedDict()
        self._item = None
        super().__init__(*args, **kwargs)

    def _build(self):
        x1, y1, x2, y2, radius = self._bounds
        w, h = max(1, round(x2 - x1)), max(1, round(y2 - y1))
        radius = max(0, min(radius, w / 2, h / 2))
        background = self.cv.cget("bg")
        key = (w, h, radius, self._fill, self._outline, self._width, background)
        photo = self._images.get(key)
        if photo is None:
            scale = 8

            def rgb(color):
                return tuple(c // 257 for c in self.cv.winfo_rgb(color))

            bitmap = Image.new("RGB", (w * scale, h * scale), rgb(background))
            ImageDraw.Draw(bitmap).rounded_rectangle(
                (0, 0, w * scale - 1, h * scale - 1),
                radius=radius * scale,
                fill=rgb(self._fill or background),
                outline=rgb(self._outline) if self._outline else None,
                width=max(1, round(self._width * scale)),
            )
            bitmap = bitmap.resize((w, h), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(bitmap, master=self.cv)
            self._images[key] = photo
            if len(self._images) > 8:
                self._images.popitem(last=False)
        self._images.move_to_end(key)
        self._photo = photo
        if self._item is None:
            self._item = self.cv.create_image(
                x1, y1, anchor="nw", image=photo, tags=self._tag,
            )
        else:
            self.cv.coords(self._item, x1, y1)
            self.cv.itemconfigure(self._item, image=photo)
        self.lower()


class RoundedButton(tk.Canvas):
    """苹果风格圆角按钮，带 hover / press 视觉反馈。

    variant:
      primary   系统蓝填充 + 白字   —— 主操作（加入队列）
      secondary 浅灰填充 + 深字     —— 常规操作（暂停 / 清空）
      outline   白底 + 描边         —— 需要更轻的常规操作
      tint      淡蓝底 + 蓝字       —— 强调但非主操作
      ghost     透明 + 蓝字         —— 最低优先级
      danger    透明 + 红字         —— 破坏性操作（删除）
    """

    _STYLES = {
        "primary":   dict(fill=T.accent,      fg="#FFFFFF", hover=T.accent_hover,
                          press=T.accent_press, line=""),
        "secondary": dict(fill=T.surface_alt, fg=T.text,   hover=T.fill_hover,
                          press=T.fill_press,   line=T.border_strong),
        "outline":   dict(fill=T.surface,     fg=T.text,   hover=T.surface_alt,
                          press=T.fill_hover,   line=T.border_strong),
        "tint":      dict(fill=T.accent_tint, fg=T.accent, hover=T.accent_tint2,
                          press="#C7E0FF",      line=""),
        # ghost/danger 必须有可见底色——透明底在白卡片上等于「只有字没有按钮」
        "ghost":     dict(fill=T.surface_alt, fg=T.accent, hover=T.fill_hover,
                          press=T.fill_press,   line=""),
        "danger":    dict(fill=T.error_tint,  fg=T.error,  hover="#FFDCDA",
                          press="#FFD0CE",      line="#FFBDB8"),
    }

    def __init__(self, master, text="", command=None, variant="secondary",
                 height=34, radius=None, padx=16, font=None, bg=None,
                 min_width=0, state="normal"):
        parent_bg = bg or _bg_of(master)
        super().__init__(master, height=height, width=max(min_width, 40),
                          highlightthickness=0, bd=0, bg=parent_bg, takefocus=0)
        super().configure(takefocus=1)
        self._command = command
        self._variant = variant if variant in self._STYLES else "secondary"
        self._style = self._STYLES[self._variant]
        self._text = text
        self._height = height
        self._radius = T.radius_button if radius is None else radius
        self._padx = padx
        self._font = font
        self._min_width = min_width
        self._state = state
        self._hover = False
        self._pressed = False
        self._focused = False

        self.configure(cursor="hand2" if state == "normal" else "arrow")
        self._shape = ButtonBackground(self, 1, 1, 2, 2, self._radius,
                            fill=self._style["fill"] or parent_bg,
                            outline=self._style["line"] or "",
                            width=1 if self._style["line"] else 0)
        self._label = self.create_text(0, 0, text=text, fill=self._style["fg"],
                                       font=self._font)
        self._relayout()

        self.bind("<Configure>", lambda e: self._relayout())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda e: self._focus(True))
        self.bind("<FocusOut>", lambda e: self._focus(False))
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)

    # --- 布局与绘制 ---
    def _relayout(self):
        if self._font is None:
            self._font = ui_font(self, 10)
            self.itemconfigure(self._label, font=self._font)
        measure = tkfont.Font(font=self._font)
        text_w = measure.measure(self._text) if self._text else 0
        w = max(self._min_width, text_w + self._padx * 2, 40)
        h = max(self._height, measure.metrics("linespace") + 14)
        super().configure(height=h)
        if int(self.cget("width")) != int(w):
            super().configure(width=int(w))
        self.itemconfigure(self._label, text=self._text, font=self._font,
                           fill=self._text_color())
        self.coords(self._label, w / 2, h / 2)
        self._shape.set_bounds(1, 1, w - 1, h - 1, self._radius)
        self._shape.config(fill=self._fill_color(),
                           outline=self._line_color() or "",
                           width=1)
        self._shape.lower()

    def _fill_color(self):
        if self._state == "disabled":
            return T.surface_alt
        if self._pressed:
            return self._style["press"] or T.accent_tint2
        if self._hover:
            return self._style["hover"] or T.accent_tint
        return self._style["fill"] or self._parent_bg()

    def _line_color(self):
        if self._state == "disabled":
            return T.border
        return T.accent if self._focused else self._style["line"] or ""

    def _text_color(self):
        if self._state == "disabled":
            return T.text3
        return self._style["fg"]

    def _parent_bg(self):
        return self.cget("bg")

    # --- 交互 ---
    def _focus(self, focused):
        self._focused = focused
        self._repaint()

    def _activate(self, _event=None):
        if self._state == "normal" and self._command:
            self._command()
        return "break"

    def _on_enter(self, _e=None):
        if self._state != "normal":
            return
        self._hover = True
        self._repaint()

    def _on_leave(self, _e=None):
        self._hover = self._pressed = False
        self._repaint()

    def _on_press(self, _e=None):
        if self._state != "normal":
            return
        self.focus_set()
        self._pressed = True
        self._repaint()

    def _on_release(self, e=None):
        if self._state != "normal":
            return
        was = self._pressed
        self._pressed = False
        self._repaint()
        inside = (e is None or
                  0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height())
        if was and inside and self._command:
            self._command()

    def _repaint(self):
        self._shape.config(fill=self._fill_color(),
                           outline=self._line_color() or "")
        self.itemconfigure(self._label, fill=self._text_color())

    # --- 对外接口（与 ttk 用法保持兼容） ---
    def configure(self, **kw):
        redraw = False
        if "text" in kw:
            self._text = kw.pop("text")
            redraw = True
        if "state" in kw:
            self._state = kw.pop("state")
            self.configure(cursor="hand2" if self._state == "normal" else "arrow")
            redraw = True
        if "variant" in kw:
            self._variant = kw.pop("variant")
            self._style = self._STYLES.get(self._variant, self._STYLES["secondary"])
            redraw = True
        if kw:
            super().configure(**kw)
        if redraw:
            self._relayout()
        return self

    config = configure

    def set_text(self, text):
        self.configure(text=text)


# ----------------------------------------------------------------------
# 圆角勾选框（✓ 而非 ×）
# ----------------------------------------------------------------------
class Checkbox(tk.Canvas):
    """自绘勾选框：未选中为浅描边圆角方框，选中为蓝色填充 + 白色对勾。

    clam 主题的 ttk.Checkbutton 用「×」表示选中，与中文用户直觉不符，
    这里改成对勾，并且整行（含文字）都可点击。
    """

    def __init__(self, master, text="", variable=None, command=None,
                 bg=None, font=None, box=18, gap=8, fg=None):
        parent_bg = bg or _bg_of(master)
        super().__init__(master, highlightthickness=0, bd=0, bg=parent_bg,
                         takefocus=0, height=box)
        self.var = variable if variable is not None else tk.BooleanVar(value=False)
        self._command = command
        self._text = text
        self._box = box
        self._gap = gap
        self._font = font
        self._fg = fg or T.text
        self._parent_bg = parent_bg
        self._hover = False

        self._box_shape = RRect(self, 1, 1, 2, 2, 5,
                                fill=T.surface, outline=T.border_strong, width=1)
        self._tick = self.create_line(0, 0, 0, 0, 0, 0, width=2,
                                      fill="#FFFFFF", capstyle="round",
                                      joinstyle="round", state="hidden")
        self._label = self.create_text(0, 0, text=text, fill=self._fg,
                                       font=self._font, anchor="w")

        self._relayout()
        self.configure(cursor="hand2")
        self.bind("<Configure>", lambda e: self._relayout())
        self.bind("<Button-1>", self._toggle)
        self.configure(takefocus=1)
        self.bind("<space>", self._toggle)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.var.trace_add("write", lambda *a: self._repaint())

    def _relayout(self):
        if self._font is None:
            self._font = ui_font(self, 10)
        b = self._box
        pad = 2
        cy = b / 2 + 1
        self._box_shape.set_bounds(pad, 1, b - pad + 1, b - 1, 5)
        # 对勾：三点点折线，比字体里的 ✓ 更锐利
        x0 = pad + b * 0.20
        x1 = pad + b * 0.42
        x2 = pad + b * 0.78
        self.coords(self._tick,
                    x0, cy,
                    x1, cy + b * 0.22,
                    x2, cy - b * 0.24)
        self.itemconfigure(self._label, font=self._font, fill=self._fg,
                           text=self._text)
        self.coords(self._label, b + self._gap, cy)
        # 宽度按文字长度自适应
        measure = tkfont.Font(font=self._font)
        w = b + self._gap + (measure.measure(self._text) if self._text else 0) + 2
        if int(self.cget("width")) != int(w):
            super().configure(width=int(w))
        self._repaint()

    def _rect_points(self, x1, y1, x2, y2, r):
        return [
            x1 + r, y1,  x2 - r, y1,  x2, y1,
            x2, y1 + r,  x2, y2 - r,  x2, y2,
            x2 - r, y2,  x1 + r, y2,  x1, y2,
            x1, y2 - r,  x1, y1 + r,  x1, y1,
        ]

    def _repaint(self):
        on = bool(self.var.get())
        if on:
            self._box_shape.config(fill=T.accent, outline=T.accent)
            self.itemconfigure(self._tick, state="normal")
        else:
            line = T.accent if self._hover else T.border_strong
            self._box_shape.config(fill=T.surface, outline=line)
            self.itemconfigure(self._tick, state="hidden")

    def _enter(self, _e=None):
        self._hover = True
        self._repaint()

    def _leave(self, _e=None):
        self._hover = False
        self._repaint()

    def _toggle(self, _e=None):
        self.var.set(not self.var.get())
        if self._command:
            self._command()


# ----------------------------------------------------------------------
# 圆角卡片 / 文本井
# ----------------------------------------------------------------------
class Card(tk.Canvas):
    """圆角卡片。内容放进 `.body`（一个普通 tk.Frame），其余交给卡片管理。

    expand=False 时卡片高度随内容自适应；expand=True 时内容填满卡片高度。

    注意：不能只靠 body 的 <Configure> 驱动高度——Canvas 初始高度过小时，
    内嵌窗口项不被映射，body 永远收不到 Configure，卡片就会卡在 1px。
    所以这里用 after_idle 主动测量 + 公开 refresh() 供外部在增删子控件后调用。
    """

    def __init__(self, master, radius=None, pad=16, bg=None, fill=None,
                 expand=False, border=True, border_color=None):
        parent_bg = bg or _bg_of(master)
        super().__init__(master, highlightthickness=0, bd=0, bg=parent_bg,
                         width=1, height=2 * pad + 8)
        self._parent_bg = parent_bg
        self._radius = T.radius_card if radius is None else radius
        self._pad = pad
        self._fill = fill or T.surface
        self._expand = expand
        self._border = border
        self._border_color = border_color or T.border
        self.body = tk.Frame(self, bg=self._fill)
        self._win = self.create_window(pad, pad, anchor="nw", window=self.body)
        self._shape = None
        self.bind("<Configure>", self._on_canvas)
        self.body.bind("<Configure>", lambda e: self.after_idle(self._sync_height))

    def _on_canvas(self, _e=None):
        w = self.winfo_width()
        if w <= 1:
            return
        self.itemconfigure(self._win, width=max(w - 2 * self._pad, 1))
        self._draw(w, self.winfo_height())
        if self._expand:
            self.itemconfigure(self._win, height=max(self.winfo_height() - 2 * self._pad, 1))
        else:
            self.after_idle(self._sync_height)

    def _sync_height(self):
        """按内容实际需求高度调整卡片高度。"""
        if self._expand:
            return
        try:
            need = self.body.winfo_reqheight() + 2 * self._pad
        except tk.TclError:
            return
        if abs(self.winfo_height() - need) > 1:
            super().configure(height=need)
            self._draw(self.winfo_width(), need)

    def refresh(self):
        """内容增删后调用，让卡片重新贴合高度。"""
        self.after_idle(self._sync_height)

    def _draw(self, w, h):
        if w <= 1 or h <= 1:
            return
        outline = self._border_color if self._border else ""
        if self._shape is None:
            self._shape = RRect(self, 0.5, 0.5, w - 0.5, h - 0.5, self._radius,
                                fill=self._fill, outline=outline, width=1)
        else:
            self._shape.set_bounds(0.5, 0.5, w - 0.5, h - 0.5, self._radius)
            self._shape.config(fill=self._fill, outline=outline)
        self._shape.lower()


class Hairline(tk.Frame):
    """发丝分隔线。"""

    def __init__(self, master, bg=None, color=None, padx=0):
        super().__init__(master, height=1, bg=color or T.border)
        self._bg = bg
        if padx:
            self.pack_configure(padx=padx)


class Pill(tk.Canvas):
    """静态圆角标签（不可点击，用于展示状态/说明）。"""

    def __init__(self, master, text="", bg=None, fill=None, fg=None,
                 font=None, padx=10, height=24, radius=12, dot=None):
        parent_bg = bg or _bg_of(master)
        super().__init__(master, height=height, highlightthickness=0, bd=0,
                         bg=parent_bg, takefocus=0)
        self._font = font or ui_font(self, 9)
        self._fill = fill or T.accent_tint
        self._fg = fg or T.accent
        self._padx = padx
        self._height = height
        self._radius = radius
        self._dot = dot
        self._text = text
        self._shape = RRect(self, 1, 1, 2, 2, radius, fill=self._fill,
                            outline="", width=0)
        self._dot_item = (self.create_oval(0, 0, 0, 0, fill=dot, outline="")
                          if dot else None)
        self._label = self.create_text(0, 0, text=text, fill=self._fg,
                                       font=self._font, anchor="w")
        self._layout()

    def _layout(self):
        measure = tkfont.Font(font=self._font)
        tw = measure.measure(self._text) if self._text else 0
        dot_w = 14 if self._dot else 0
        w = tw + dot_w + self._padx * 2
        h = max(self._height, measure.metrics('linespace') + 8)
        super().configure(height=h)
        if int(self.cget("width")) != int(w):
            super().configure(width=int(w))
        self._shape.set_bounds(1, 1, w - 1, h - 1, self._radius)
        x = self._padx
        if self._dot:
            cy = h / 2
            self.coords(self._dot_item, x, cy - 3, x + 6, cy + 3)
            x += dot_w
        self.coords(self._label, x, h / 2)
        self._shape.lower()

    def _pts(self, x1, y1, x2, y2, r):
        r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        return [
            x1 + r, y1,  x2 - r, y1,  x2, y1,
            x2, y1 + r,  x2, y2 - r,  x2, y2,
            x2 - r, y2,  x1 + r, y2,  x1, y2,
            x1, y2 - r,  x1, y1 + r,  x1, y1,
        ]


# ----------------------------------------------------------------------
# 圆角输入框
# ----------------------------------------------------------------------
class TextField(tk.Canvas):
    """圆角输入框：Canvas 画底与描边，内部嵌一个无边框 Entry。

    聚焦时描边变系统蓝（Apple 的焦点态），失焦回到中性描边。
    外部要绑回车等事件请用 `field.entry.bind(...)`。
    """

    def __init__(self, master, textvariable=None, width=220, height=34,
                 radius=None, bg=None, font=None):
        parent_bg = bg or _bg_of(master)
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, bd=0, bg=parent_bg)
        self._radius = T.radius_well if radius is None else radius
        self._font = font or ui_font(self, 10)
        self._shape = RRect(self, 1, 1, width - 2, height - 2,
                            self._radius, fill=T.surface,
                            outline=T.border_input, width=1)
        self.var = textvariable if textvariable is not None else tk.StringVar()
        self.entry = tk.Entry(
            self, textvariable=self.var, relief="flat", bd=0,
            highlightthickness=0, bg=T.surface, fg=T.text,
            insertbackground=T.accent, font=self._font,
        )
        self._win = self.create_window(12, height / 2, anchor="w",
                                       window=self.entry,
                                       width=max(width - 24, 1),
                                       height=max(height - 12, 1))
        self.bind("<Configure>", self._on_resize)
        self.entry.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._set_focus(False))
        # 点 Canvas 空白处也把焦点交给输入框
        self.bind("<Button-1>", lambda e: self.entry.focus_set())

    def _on_resize(self, _e=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        self._shape.set_bounds(1, 1, w - 2, h - 2, self._radius)
        self.coords(self._win, 12, h / 2)
        needed = tkfont.Font(font=self._font).metrics("linespace") + 14
        if h < needed:
            self.configure(height=needed)
            return
        self.itemconfigure(self._win, width=max(w - 24, 1), height=max(h - 12, 1))

    def _rect_points(self, w, h):
        r = max(0, min(self._radius, w / 2, h / 2))
        return [
            0.5 + r, 0.5,  w - 0.5 - r, 0.5,  w - 0.5, 0.5,
            w - 0.5, 0.5 + r,  w - 0.5, h - 0.5 - r,  w - 0.5, h - 0.5,
            w - 0.5 - r, h - 0.5,  0.5 + r, h - 0.5,  0.5, h - 0.5,
            0.5, h - 0.5 - r,  0.5, 0.5 + r,  0.5, 0.5,
        ]

    def _set_focus(self, on):
        self._shape.config(outline=T.accent if on else T.border_input,
                           width=2 if on else 1)

    def get(self):
        return self.var.get()

    def set(self, value):
        self.var.set(value)

    def focus_set(self):
        self.entry.focus_set()


# ----------------------------------------------------------------------
# 下拉菜单
# ----------------------------------------------------------------------
def popup_menu(widget, entries, font=None, min_width=0):
    """在控件正下方弹出选择菜单。

    entries: [(label, callback), ...]；label 以 "-" 开头表示禁用项。
    """
    m = tk.Menu(widget, tearoff=0, font=font or ui_font(widget, 10),
                bg=T.surface, fg=T.text, activebackground=T.accent_tint,
                activeforeground=T.accent, bd=1, relief="solid")
    for label, cb in entries:
        if label.startswith("-"):
            m.add_command(label=label.lstrip("- "),
                          state="disabled",
                          foreground=T.text3)
        else:
            m.add_command(label=label, command=cb)
    x = widget.winfo_rootx()
    y = widget.winfo_rooty() + widget.winfo_height() + 4
    try:
        m.tk_popup(x, y)
    finally:
        m.grab_release()
    return m
