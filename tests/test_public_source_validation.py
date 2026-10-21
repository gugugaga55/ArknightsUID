"""实网检查器必须拒绝非空但残缺的数据，避免检查工具本身误报成功。"""

import pytest
from unittest.mock import AsyncMock

from tools.check_public_sources import check, validate_recruit, validate_rows


@pytest.mark.parametrize("rows", [None, [], {}, [None], [{"name": "干员"}]])
def test_rows_reject_empty_or_missing_fields(rows):
    with pytest.raises(ValueError):
        validate_rows(rows, None, ("name", "rarity"))


def test_rows_validate_every_record():
    with pytest.raises(ValueError, match="second"):
        validate_rows(
            {"items": {"first": {"itemId": "1"}, "second": {}}}, "items", ("itemId",)
        )


def test_zero_and_null_are_not_missing_fields():
    assert validate_rows([{"count": 0, "end": None}], None, ("count", "end")) == 1


def recruit_response():
    return {
        "query": {
            "results": {
                "银灰": {
                    "printouts": {"稀有度": [5], "职业": ["近卫"], "位置": ["近战位"]}
                },
                "芬": {
                    "printouts": {"稀有度": [2], "职业": ["先锋"], "位置": ["近战位"]}
                },
            }
        }
    }


def recruits():
    return [
        {"name": "银灰", "rarity": 6, "all_tags": ["近卫", "近战位"]},
        {"name": "芬", "rarity": 3, "all_tags": ["先锋", "近战位"]},
    ]


def test_recruit_accepts_complete_members():
    validate_recruit(recruits(), recruit_response())


@pytest.mark.parametrize(
    "kind", ["omitted", "duplicate", "rarity", "tags", "continuation"]
)
def test_recruit_detects_incomplete_or_inaccurate_parsing(kind):
    entries, response = recruits(), recruit_response()
    if kind == "omitted":
        entries.pop()
    elif kind == "duplicate":
        entries[1] = entries[0]
    elif kind == "rarity":
        entries[0]["rarity"] = 5
    elif kind == "tags":
        entries[0]["all_tags"] = ["近卫"]
    else:
        response["query-continue-offset"] = 500
    with pytest.raises(ValueError):
        validate_recruit(entries, response)


@pytest.mark.asyncio
@pytest.mark.parametrize("code, expected", [(0, True), (10002, False)])
async def test_selected_public_source_checks_envelope_without_fetching_other_tables(
    monkeypatch, capsys, code, expected
):
    from ArknightsUID.utils import http
    from ArknightsUID.utils.gamedata import _table_cache as tables
    from ArknightsUID.utils.skland import web

    fetch = AsyncMock(
        return_value={"code": code, "data": {"list": [{"id": 2, "title": "明日方舟"}]}}
    )
    unselected = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr(web, "web_public_get", fetch)
    monkeypatch.setattr(tables, "ensure_char_table", unselected)
    monkeypatch.setattr(http, "aclose", close)
    assert await check({"avatar_categories"}) is expected
    fetch.assert_awaited_once_with("/web/v1/user/avatar/category/list", {})
    unselected.assert_not_awaited()
    close.assert_awaited_once()
    output = capsys.readouterr().out
    assert '"source": "avatar_categories"' in output
    assert '"source": "characters"' not in output


@pytest.mark.asyncio
async def test_unknown_source_is_rejected_before_network():
    with pytest.raises(ValueError, match="source_typo"):
        await check({"source_typo"})


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["avatar_categories", "arknights_avatars"])
async def test_partial_avatar_catalog_is_not_reported_as_success(
    monkeypatch, capsys, source
):
    from ArknightsUID.utils import http
    from ArknightsUID.utils.skland import web

    monkeypatch.setattr(
        web,
        "web_public_get",
        AsyncMock(
            return_value={
                "code": 0,
                "data": {
                    "hasMore": True,
                    "list": [{"id": 2, "title": "明日方舟", "url": "https://example.com/a"}],
                },
            }
        ),
    )
    close = AsyncMock()
    monkeypatch.setattr(http, "aclose", close)

    assert await check({source}) is False
    assert "尚未返回完整数据" in capsys.readouterr().out
    close.assert_awaited_once()
