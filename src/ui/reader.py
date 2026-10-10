"""Article typography over existing TXT files; never changes stored content."""
from dataclasses import dataclass
import re
import tkinter.font as tkfont
from tkinter import scrolledtext

from . import kit as ui


_HEADING = re.compile(r'^(#{1,6})[ \t]+(.+)$')
_TIMESTAMP = re.compile(r'^\[([\d:]+(?:[.–—-][\d:]+)?)\]\s*(.*)$')
_LIST = re.compile(r'^(?:[-*•]\s+|\d+[.、]\s*)')
_METADATA = ('来源：', '平台：', '整理方式：')


@dataclass(frozen=True)
class ReaderBlock:
    text: str
    style: str = 'body'
    timestamp: str = ''


@dataclass(frozen=True)
class ReaderLayout:
    title: str
    metadata: tuple[str, ...]
    blocks: tuple[ReaderBlock, ...]

    @property
    def copy_text(self):
        header = '\n'.join((self.title, *self.metadata))
        body = '\n\n'.join(
            f'[{b.timestamp}]\n{b.text}' if b.timestamp else b.text
            for b in self.blocks)
        return '\n\n'.join(part for part in (header, body) if part)


def _join_lines(lines):
    """Unwrap within a paragraph, retaining English word boundaries."""
    text = ''
    for line in lines:
        value = line.strip()
        if (text and value and text[-1].isascii() and value[0].isascii() and value[0].isalnum()
                and (text[-1].isalnum() or text[-1] in ',;:.!?')):
            text += ' '
        text += value
    return text


def parse_document(text, fallback_title=''):
    """Only header syntax and hard wraps are interpreted; blank lines remain boundaries."""
    lines = text.lstrip('\ufeff').splitlines()
    title = fallback_title
    if lines:
        first = _HEADING.match(lines[0].strip())
        if first and len(first[1]) == 1:
            title = first[2].strip()
            lines = lines[1:]
    metadata, blocks, paragraph = [], [], []
    timestamp = ''
    preamble = True

    def flush():
        if paragraph or timestamp:
            blocks.append(ReaderBlock(_join_lines(paragraph), timestamp=timestamp))
            paragraph.clear()

    for line in lines:
        value = line.strip()
        if preamble and value.startswith(_METADATA):
            metadata.append(value)
            continue
        if not value:
            flush()
            timestamp = ''
            continue
        preamble = False
        heading = _HEADING.match(value)
        stamp = _TIMESTAMP.match(value)
        if heading:
            flush()
            timestamp = ''
            blocks.append(ReaderBlock(heading[2].strip(), 'heading'))
        elif stamp:
            flush()
            timestamp = stamp[1]
            paragraph.append(stamp[2])
        elif _LIST.match(value):
            flush()
            timestamp = ''
            blocks.append(ReaderBlock(value))
        else:
            paragraph.append(line)
    flush()
    return ReaderLayout(title, tuple(metadata), tuple(blocks))


class TranscriptReader(scrolledtext.ScrolledText):
    """Selectable, read-only article with responsive margins and plain-text copying."""

    def __init__(self, parent):
        families = set(tkfont.families(parent))
        family = next((name for name in ('Microsoft YaHei', '微软雅黑', 'Microsoft YaHei UI')
                       if name in families), ui.ui_font(parent, 13)[0])
        self.body_font = tkfont.Font(root=parent, family=family, size=13)
        self.column_width = self.body_font.measure('汉') * 42
        super().__init__(parent, wrap='word', relief='flat', bd=0, width=28, height=10,
                         font=self.body_font, bg=ui.T.surface, fg='#333338',
                         padx=24, pady=18, spacing1=0, spacing2=5, spacing3=16,
                         selectbackground='#C7E0FF', selectforeground='#1D1D1F',
                         inactiveselectbackground='#DFEAF7', cursor='xterm', undo=False)
        self.tag_configure('title', font=(family, 20, 'bold'), foreground=ui.T.text,
                           spacing1=0, spacing2=4, spacing3=12, wrap='char')
        self.tag_configure('metadata', font=(family, 9), foreground=ui.T.text2,
                           spacing1=0, spacing2=3, spacing3=5)
        self.tag_configure('heading', font=(family, 15, 'bold'), foreground=ui.T.text,
                           spacing1=18, spacing2=3, spacing3=10, wrap='char')
        self.tag_configure('timestamp', font=(family, 10), foreground='#5684B0',
                           spacing1=12, spacing2=2, spacing3=6)
        self.copy_text = ''
        self.row_times = {}
        self.bind('<Configure>', self._layout)
        self.configure(state='disabled')

    def _layout(self, event):
        # Text's native wrapping handles long URLs and narrow viewports. Only
        # whitespace changes here, so resizing does not reset selection/scroll.
        padding = max(18, (event.width - self.column_width) // 2)
        if int(self.cget('padx')) != padding:
            self.configure(padx=padding)

    def show(self, layout):
        self.configure(state='normal')
        self.delete('1.0', 'end')
        self.row_times = {}
        self.copy_text = layout.copy_text
        if layout.title:
            self.insert('end', layout.title + '\n', 'title')
        for metadata in layout.metadata:
            self.insert('end', metadata + '\n', 'metadata')
        for block in layout.blocks:
            start = int(self.index('end-1c').split('.')[0])
            if block.timestamp:
                self.insert('end', f'[{block.timestamp}]\n', 'timestamp')
            if block.text:
                self.insert('end', block.text + '\n', block.style)
            end = int(self.index('end-1c').split('.')[0])
            for row in range(start, end):
                self.row_times[row] = block.timestamp
        self.configure(state='disabled')
        self.yview_moveto(0)

    def show_message(self, title, text):
        self.show(ReaderLayout(title, (), (ReaderBlock(text),)))
        self.copy_text = ''
