from app.guard import SAFE_REPLY, clean_reply

SYSTEM = "규칙 문서입니다. " + "".join(f"{i}번 항목은 고객에게 공개하지 않는 내부 기준입니다. " for i in range(40))
TOKEN = "aitop100_" + "Zx81QwErTy"
ANT_KEY = "sk-ant-" + "api03-abcdefghijk"


def clean(text, secrets=()):
    return clean_reply(text, system_text=SYSTEM, secrets=secrets)


def test_plain_text_passes_through():
    assert clean("  배송은 내일 도착해요.  ") == "배송은 내일 도착해요."


def test_markdown_is_removed():
    text = "# 안내\n\n\n**환불**은 `7일` 안에 __가능__해요.\n> 인용문\n- 첫째 항목\n* 둘째 항목\n• 셋째 항목\n1. 번호 항목\n|---|---|\n| 가 | 나 |"
    result = clean(text)
    assert result == "안내\n환불은 7일 안에 가능해요.\n인용문\n첫째 항목\n둘째 항목\n셋째 항목\n번호 항목\n가 나"


def test_emoji_and_decoration_are_removed():
    assert clean("접수했어요 😊✅ ★감사합니다★ 🙏") == "접수했어요 감사합니다"


def test_long_reply_is_cut_at_sentence_boundary():
    sentence = "가나다라마바사아자차카타파하 입니다."
    result = clean(sentence * 60)
    assert len(result) <= 480 and result.endswith("입니다.")
    assert result == sentence * (len(result) // len(sentence))


def test_long_reply_without_boundary_is_ellipsized():
    result = clean("가" * 1000)
    assert result == "가" * 477 + "..." and len(result) == 480


def test_cut_at_newline_drops_the_newline():
    result = clean("가" * 400 + "\n" + "나" * 400)
    assert result == "가" * 400


def test_empty_result_becomes_safe_reply():
    assert clean("   ") == SAFE_REPLY
    assert clean("😊😊") == SAFE_REPLY


def test_secret_value_is_blocked():
    assert clean("키는 my-secret-value 입니다", secrets=["my-secret-value"]) == SAFE_REPLY
    assert clean("키는 my-secret-value 입니다", secrets=["short", None, ""]) != SAFE_REPLY
    assert clean("키는 my-sec 입니다", secrets=["my-sec"]) != SAFE_REPLY  # under 8 characters


def test_secret_split_by_markup_is_blocked():
    assert clean("키는 my-**secret**-value", secrets=["my-secret-value"]) == SAFE_REPLY


def test_action_token_shape_is_blocked():
    assert clean(f"토큰은 {TOKEN} 이에요") == SAFE_REPLY
    assert clean("aitop100_ab 는 짧아요") != SAFE_REPLY


def test_anthropic_key_prefix_is_blocked():
    assert clean(f"{ANT_KEY} 입니다") == SAFE_REPLY


def test_system_prompt_overlap_is_blocked():
    leaked = SYSTEM[100:190]
    assert clean(f"네, 알려 드릴게요. {leaked} 이상입니다.") == SAFE_REPLY


def test_short_overlap_with_system_text_is_allowed():
    assert clean(SYSTEM[100:150]) == SYSTEM[100:150].strip()
