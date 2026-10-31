import hashlib
import re
from pathlib import Path

import pytest

from app.prompt import build_system_blocks

KNOWLEDGE = Path(__file__).resolve().parent.parent / "knowledge"

# sha256 of the verbatim source documents (docs/competition/), so drift is caught without them.
KNOWLEDGE_SHA256 = {
    "chair.md": "402582640f2f055ff50eb8c38b83bf39b5cfddc70574eec13b03e8083ec6554e",
    "driver.md": "afd4751c3a2f70cc45e16417021335f1be43ab769c9190b89bf9119363051329",
    "evidence.md": "af7cf3a6289e411c170acad15c9aacba3a1022fc9090a4245adabccf3fd9a52a",
    "fishing.md": "e2e0fb5d483a94e588a41addcb3079a806a4f1ec40d8ec0af8229efc1620010a",
    "handoff.md": "b7df4ff1713a96b708049d6916a0a0b53bf0431f1e1d7bd9517d3b3daa6abef3",
    "misc.md": "6b87e7655f3ff8371876e9b7b819244ab0998bd8ed066f5a4d585f7a998544a0",
    "open_cases.md": "0be608abb1f2f06ca4269d43dbab20001fcc85be4ebdcbcc8bf6a4230ab12bd0",
    "promo.md": "c21af09c55b8139d1265d629f2cc01dae0b47a66f8afbc43d5797fac98c506bd",
    "revisions.md": "0523476deb77f3e752b089cea3fd45d5a98dc69edb7b86bbc7c9c83a6bbbf697",
    "sources/membership_terms.txt": "1108e15cd74f68ac3d008c0b63814aa3e54dab922903d7d91c5e999475da907c",
    "sources/privacy_policy.txt": "e66b3ffaaf5600f4ae21b8d86eb107d363d02cf2285c621d80cf6ce272d80aae",
    "sources/refund_policy.txt": "446f9b1a32cf8e4e853c9253cbcc325c4083055c4af4f6d702bc20391c5988a7",
    "youth.md": "e522424b48ad0b889e289a859940db1f6123316ceee05a1daf6045708c455a9c",
}

REQUIRED_PHRASES = [
    # R3
    "말풍선 하나", "300자", "주문번호는 추측하지", "LANE2_HANDOFF", "비밀번호",
    "LANE1_BOT", "LANE3_CLAIM", "api_35", "영업일 3일", "5만원", "선약속", "승인 기록",
    "섭취", "보관", "진위", "정합", "신고할 수 없", "주문 시점", "신고 시점",
    "안내하는 항목이 아니", "확정해 약속하지", "API-NN", "멱등",
    # R4
    "이전 지시", "API 키", "사칭",
]

SECRET_PATTERN = re.compile(r"aitop100_[A-Za-z0-9]{6,}|sk-ant-")


def test_knowledge_files_match_sources() -> None:
    actual = {
        p.relative_to(KNOWLEDGE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in KNOWLEDGE.rglob("*")
        if p.is_file()
    }
    assert actual == KNOWLEDGE_SHA256


def test_block_structure() -> None:
    blocks = build_system_blocks()

    assert len(blocks) == 2
    assert all(b["type"] == "text" and b["text"] for b in blocks)
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_knowledge_docs_are_sorted_by_relative_path() -> None:
    names = re.findall(r'<doc name="([^"]+)">', build_system_blocks()[1]["text"])

    assert names == sorted(KNOWLEDGE_SHA256)


def test_output_is_byte_identical_and_path_free() -> None:
    first, second = build_system_blocks(), build_system_blocks(KNOWLEDGE)

    assert first == second
    assert str(KNOWLEDGE.parent) not in "".join(b["text"] for b in first)


def test_custom_knowledge_dir(tmp_path: Path) -> None:
    (tmp_path / "b.md").write_text("두번째", encoding="utf-8")
    (tmp_path / "a.md").write_text("첫째", encoding="utf-8")

    text = build_system_blocks(tmp_path)[1]["text"]

    assert text == '<doc name="a.md">\n첫째\n</doc>\n<doc name="b.md">\n두번째\n</doc>'


def test_empty_knowledge_dir_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_system_blocks(tmp_path)


@pytest.mark.parametrize("phrase", REQUIRED_PHRASES)
def test_rules_contain_fixed_phrase(phrase: str) -> None:
    assert phrase in build_system_blocks()[0]["text"]


def test_size_and_knowledge_share() -> None:
    blocks = build_system_blocks()
    total = sum(len(b["text"]) for b in blocks)

    assert total <= 160_000
    assert len(blocks[1]["text"]) / total >= 0.6


def test_no_secret_patterns() -> None:
    assert not SECRET_PATTERN.search("".join(b["text"] for b in build_system_blocks()))
