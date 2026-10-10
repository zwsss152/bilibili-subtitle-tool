"""Serializable tasks; each page owns its progress and outputs."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
import uuid
import re
from ..paths import PROJECT_ROOT, OUTPUT_ROOT
from ..core.textout import sanitize_filename
from ..core.library import ORIGINAL

DONE = {"完成", "失败", "已取消"}


@dataclass
class Page:
    number: int
    title: str
    url: str
    duration: float = 0
    status: str = "排队中"
    progress: int = 0
    txt: str = ""
    raw: str = ""
    error: str = ""
    reading: str = ""
    reading_status: str = "未整理"
    reading_error: str = ""
    reading_requested: bool = False


@dataclass
class Task:
    jid: str
    kind: str
    ref: str
    source_url: str
    topic: str
    output_dir: str
    selected_page: int | None = None
    title: str = ""
    status: str = "排队中"
    detail: str = ""
    error: str = ""
    pages: list[Page] = field(default_factory=list)
    cancelled: bool = False
    engine: str = 'local'

    @classmethod
    def create(cls, source, topic, engine='local'):
        if engine not in ('local', 'cloud'):
            raise ValueError('请选择本地或在线方式')
        topic = sanitize_filename(topic or "未分类")
        return cls(uuid.uuid4().hex, source.kind, source.ref, source.url, topic,
                   str(Path("文字稿") / ORIGINAL / topic), source.page, source.url, engine=engine)

    @property
    def key(self):
        return self.kind, self.ref, self.selected_page

    @property
    def progress(self):
        if self.status == "完成":
            return 100
        if not self.pages:
            return 0
        return int(sum(100 if p.status in DONE else p.progress for p in self.pages) / len(self.pages))

    def update_status(self):
        if self.cancelled:
            self.status = "已取消"
        elif not self.pages:
            return
        elif all(p.status in DONE for p in self.pages):
            failures = [p for p in self.pages if p.status == "失败"]
            self.status = "部分失败" if failures and len(failures) < len(self.pages) else "失败" if failures else "完成"
            self.detail = f"{len(failures)} 个分 P 失败，可重试" if failures else f"已保存至 {self.topic}"
        else:
            self.status = next((stage for stage in ("识别中", "下载中", "解析中", "等待识别", "排队中")
                                if any(p.status == stage for p in self.pages)), "排队中")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, values, restore=True):
        fields = cls.__dataclass_fields__
        task = cls(**{k: v for k, v in values.items() if k in fields and k != "pages"})
        if task.engine not in ('local', 'cloud'):
            raise ValueError('任务识别方式无效')
        task.pages = [Page(**{k: v for k, v in page.items() if k in Page.__dataclass_fields__}) for page in values.get("pages", [])]
        if restore:
            task.cancelled = False
            for page in task.pages:
                if page.status not in DONE:
                    page.status, page.progress = "排队中", 0
                if page.status == "完成" and not (PROJECT_ROOT / page.txt).is_file():
                    page.status, page.progress = "失败", 0
                    page.error = '原稿已被外部删除；点击重试可重新生成'
            if task.status not in ("完成", "失败", "部分失败"):
                task.status = "排队中"
            for page in task.pages:
                if not page.reading_requested and (page.reading_status in ('待整理','写入中') or page.reading_status.startswith('整理中')):
                    page.reading_status = '未整理'
            task.update_status()
            if task.status == '完成' and any(p.reading_status == '未整理' for p in task.pages):
                task.detail = '原稿已保存；点击整理生成阅读稿'
        return task


def migrate_legacy(values, default_topic):
    """Keep completed outputs from version 1; recover unknown pending pages once parsed."""
    kind = values.get("kind", "bilibili")
    ref = values.get("ref", "")
    if isinstance(ref, list):
        ref = ":".join(map(str, ref))
    topic = sanitize_filename(default_topic or "未分类")
    task = Task(str(values.get("jid") or uuid.uuid4().hex), kind, ref, values.get("raw", ""), topic,
                str(Path("文字稿") / topic), title=values.get("title", ""))
    for index, old_path in enumerate(values.get("saved", []), 1):
        path = Path(old_path)
        parts = path.parts
        if "文字稿" in parts:
            local = Path(*parts[parts.index("文字稿"):])
        else:
            local = Path("文字稿") / topic / path.name
        if (PROJECT_ROOT / local).exists():
            task.topic = local.parts[2] if len(local.parts)>3 and local.parts[1]==ORIGINAL else local.parts[1]
            task.output_dir = str(local.parent)
            match = re.search(r'(?:_P|\[P)(\d+)', path.stem, re.IGNORECASE)
            number = int(match.group(1)) if match else index
            task.pages.append(Page(number, path.stem, task.source_url, status="完成", progress=100, txt=str(local)))
    if kind not in ("bilibili", "gcores", 'douyin'):
        task.status, task.error = "失败", "新版仅支持 B 站和机核；旧记录已保留"
    elif values.get("status") == "完成" and task.pages:
        task.status = "完成"
    elif values.get("status") == "失败":
        task.status, task.error = "失败", values.get("error", "旧任务失败，可重试")
    else:
        # Partial v1 tasks lack page identifiers; retain files, resolve remaining pages afresh.
        task.status = "排队中"
    return task
