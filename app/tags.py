"""文件标签的解析、归一化与持久化序列化。

标签是**与存储路径无关的元数据**：只登记在 ``ft_file_record`` 表的 ``tags`` 文本列
（JSON 数组），不参与目录命名、OBS 对象键或 ``relpath``，因此不会影响下载 / 删除的
路径契约。两种存储后端共用同一套逻辑。

对外只有三种入口：

- 上传表单字段 ``tags``：逗号分隔字符串（中英文逗号均可），走 ``parse_tags_form``；
- 修改标签 JSON 体 ``{"tags": [...]}``：字符串数组，走 ``normalize_tags``；
- 入库 / 出库：``encode_tags`` / ``decode_tags``。
"""

from __future__ import annotations

import json
import re
from typing import Iterable, List, Optional

from app.config import Settings, settings
from app.errors import ValidationError

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
#: 表单里允许的中英文逗号，二者等价。
_FORM_SEPARATORS = (",", "，")


def normalize_tags(values: Iterable[str], cfg: Optional[Settings] = None) -> List[str]:
    """校验并归一化标签列表：去空白、丢空项、去重（保序）、校验长度与数量。

    非法标签一律 422 ``validation_error``，不做猜测性截断；只有空项会被静默丢弃。
    """
    if isinstance(values, str):  # 避免把字符串当字符序列遍历
        raise TypeError("normalize_tags 需要字符串列表；表单字段请用 parse_tags_form")
    config = cfg or settings
    result: List[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValidationError("标签必须是字符串", details={"tag": repr(raw)})
        value = raw.strip()
        if not value:
            continue
        if _CONTROL_RE.search(value):
            raise ValidationError("标签不能包含控制字符", details={"tag": raw})
        if len(value) > config.max_tag_length:
            raise ValidationError(
                f"标签长度不能超过 {config.max_tag_length} 个字符",
                details={"tag": raw, "max_length": config.max_tag_length},
            )
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) > config.max_tags_per_file:
            raise ValidationError(
                f"单个文件最多 {config.max_tags_per_file} 个标签",
                details={"max_tags": config.max_tags_per_file},
            )
    return result


def parse_tags_form(raw: str, cfg: Optional[Settings] = None) -> List[str]:
    """把上传表单的 ``tags`` 字段拆成标签列表。

    中英文逗号都当分隔符；连续分隔符产生的空项会被丢弃。留空 / 不传表示无标签。
    """
    if not raw:
        return []
    value = raw
    for separator in _FORM_SEPARATORS:
        value = value.replace(separator, "\n")
    return normalize_tags(value.split("\n"), cfg)


def encode_tags(tags: Iterable[str]) -> str:
    """序列化为 JSON 文本；``ensure_ascii=False`` 让中文标签在库里可读。"""
    return json.dumps(list(tags), ensure_ascii=False, separators=(",", ":"))


def decode_tags(raw: Optional[str]) -> List[str]:
    """反序列化；历史脏数据 / 损坏内容一律退化为 []，不让列表接口 500。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
