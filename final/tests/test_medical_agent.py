"""Unit and integration tests for the Medical Copilot system.

Verifies:
1. S0/S1 Emergency red-line interception (ACS, Stroke, Anaphylaxis, Hemorrhage).
2. Deterministic clinical calculators (BMI, Cockcroft-Gault eGFR, CHA2DS2-VASc).
3. Deterministic Drug-Drug Interaction (DDI) & allergy cross-reactivity screening.
4. Active triage history taking, slot extraction, and missing slot follow-up.
5. Structured SOAP clinical note generation and document library integration.
6. UnifiedAgent planning, tool execution, and guardrail enforcement.
"""

import json
from unittest.mock import MagicMock

import pytest

from internal.application.medical import (
    calculate_bmi,
    calculate_cha2ds2_vasc,
    calculate_gfr,
    check_drug_safety,
    check_emergency_redline,
    generate_soap_note,
    MedicalService,
)
from internal.application.medical_agent import (
    MEDICAL_TOOL_NAME,
    build_medical_copilot_tool,
    is_medical_query,
    parse_medical_request,
)


def test_document_write_flag_rejects_truthy_strings():
    assert parse_medical_request({'save_document': False}).save_document is False
    for value in ('False', 'false', 'true', 1, {}):
        with pytest.raises(ValueError, match='boolean'):
            parse_medical_request({'save_document': value})
    with pytest.raises(ValueError, match='object'):
        parse_medical_request({'score_params': '{"weight_kg": 75}'})
    assert build_medical_copilot_tool(MedicalService(), 'test-user').side_effecting is True


# ---------------------------------------------------------------------------
# 1. 急危重症 S0/S1 硬门禁测试
# ---------------------------------------------------------------------------

def test_emergency_redline_acs_heart_attack():
    # 压榨性胸痛向左肩放射
    res = check_emergency_redline("患者突发胸骨后压榨性胸痛，向左肩放射伴大汗淋漓")
    assert res is not None
    assert res["triggered"] is True
    assert res["severity"] == "S0"
    assert "心肌梗死" in res["condition_name"] or "冠脉" in res["condition_name"]
    assert "120" in res["action_guide"]


def test_emergency_redline_stroke_fast():
    # FAST 中风：口角歪斜 + 一侧肢体麻木
    res = check_emergency_redline("老人突发口角歪斜，右侧一侧肢体无力，说话吐字不清")
    assert res is not None
    assert res["triggered"] is True
    assert res["severity"] == "S0"
    assert "卒中" in res["condition_name"] or "中风" in res["condition_name"]
    assert "120" in res["action_guide"]


def test_emergency_redline_anaphylaxis():
    # 喉头水肿与过敏休克
    res = check_emergency_redline("吃海鲜后突发喉头水肿，憋气全身荨麻疹")
    assert res is not None
    assert res["triggered"] is True
    assert res["severity"] == "S0"
    assert "休克" in res["condition_name"] or "喉头水肿" in res["condition_name"]


def test_emergency_redline_massive_hemorrhage():
    # 大出血
    res = check_emergency_redline("胃溃疡病史，突然大量呕血伴头晕出虚汗")
    assert res is not None
    assert res["triggered"] is True
    assert res["severity"] == "S0"


def test_emergency_redline_safe_query():
    # 普通慢性症状不触发 S0 红线
    res = check_emergency_redline("有点轻微感冒流鼻涕，嗓子有一点痒")
    assert res is None


# ---------------------------------------------------------------------------
# 2. 确定性临床医学计算器测试
# ---------------------------------------------------------------------------

def test_calculate_bmi():
    # 正常体重: 175cm, 68kg -> 22.2
    normal = calculate_bmi(175, 68)
    assert normal["value"] == 22.2
    assert "正常" in normal["category"]

    # 超重: 170cm, 75kg -> 25.95
    overweight = calculate_bmi(170, 75)
    assert overweight["value"] == 25.95
    assert "超重" in overweight["category"]

    # 肥胖: 165cm, 85kg -> 31.22
    obese = calculate_bmi(165, 85)
    assert obese["value"] == 31.22
    assert "肥胖" in obese["category"]

    # 异常输入
    with pytest.raises(ValueError):
        calculate_bmi(-170, 60)


def test_calculate_gfr_cockcroft_gault():
    # 男性 60岁, 70kg, Scr 100 umol/L
    # Ccr = ((140-60)*70) / (0.818 * 100) = 5600 / 81.8 = 68.46 ml/min
    res_m = calculate_gfr(60, 70, 100, sex="male")
    assert res_m["value"] == 68.46
    assert "CKD 2期" in res_m["stage"]

    # 女性 (乘以 0.85): 68.46 * 0.85 = 58.19 ml/min
    res_f = calculate_gfr(60, 70, 100, sex="female")
    assert res_f["value"] == 58.19
    assert "CKD 3期" in res_f["stage"]


def test_calculate_cha2ds2_vasc():
    # 76岁男性 (+2分), 心衰 (+1分), 高血压 (+1分) -> 4分
    res = calculate_cha2ds2_vasc(
        age=76,
        sex="male",
        congestive_heart_failure=True,
        hypertension=True,
        diabetes=False,
        stroke_or_tia=False,
    )
    assert res["score"] == 4
    assert "强烈推荐口服抗凝药" in res["anticoagulation_recommendation"]

    # 25岁女性 (仅女性+1分), 无其他危险因素 -> 1分 (低危无需抗凝)
    low_risk = calculate_cha2ds2_vasc(age=25, sex="female")
    assert low_risk["score"] == 1
    assert "无需抗凝" in low_risk["anticoagulation_recommendation"]


# ---------------------------------------------------------------------------
# 3. 处方用药安全与配伍禁忌 (DDI & 过敏)
# ---------------------------------------------------------------------------

def test_drug_safety_ddi_warfarin_aspirin():
    # 华法林 + 阿司匹林 -> 高出血风险
    res = check_drug_safety(drugs=["华法林", "阿司匹林"])
    assert res["is_safe"] is False
    assert len(res["interaction_conflicts"]) >= 1
    assert any("出血" in conf["mechanism"] for conf in res["interaction_conflicts"])


def test_drug_safety_s0_nitro_sildenafil():
    # 硝酸甘油 + 西地那非 -> S0 绝对禁忌
    res = check_drug_safety(drugs=["单硝酸异山梨酯", "西地那非"])
    assert res["has_s0_contraindication"] is True
    assert any(conf["severity"] == "S0" for conf in res["interaction_conflicts"])


def test_drug_safety_allergy_penicillin():
    # 青霉素过敏 -> 严禁开具阿莫西林
    res = check_drug_safety(drugs=["阿莫西林"], allergies=["青霉素"])
    assert res["is_safe"] is False
    assert res["has_s0_contraindication"] is True
    assert len(res["allergy_alerts"]) >= 1
    assert "过敏" in res["allergy_alerts"][0]["warning"]


def test_drug_safety_asthma_contraindication():
    # 哮喘患者禁用普萘洛尔 (β阻滞剂)
    res = check_drug_safety(drugs=["普萘洛尔"], patient_conditions=["支气管哮喘"])
    assert res["is_safe"] is False
    assert len(res["condition_alerts"]) >= 1
    assert "哮喘" in res["condition_alerts"][0]["warning"]


# ---------------------------------------------------------------------------
# 4. 门诊 SOAP 病历生成
# ---------------------------------------------------------------------------

def test_generate_soap_note():
    patient = {"name": "王医生模拟患者", "age": "52", "gender": "女"}
    subj = {"chief_complaint": "反复胸痛伴背部酸胀2周", "allergies": "青霉素过敏"}
    obj = {"vitals": {"血压": "135/85 mmHg"}, "physical_exam": "心界正常"}
    assess = {"primary_diagnosis": "劳力性心绞痛待排查"}
    plan = {"treatment": ["阿司匹林口服", "定期复查心电图"]}

    md = generate_soap_note(patient, subj, obj, assess, plan, evidence_ids=["EVID_GUIDELINE_ACS_2024"])
    assert "# 门诊结构化医疗病历记录 (SOAP Note)" in md
    assert "王医生模拟患者" in md
    assert "青霉素过敏" in md
    assert "EVID_GUIDELINE_ACS_2024" in md


# ---------------------------------------------------------------------------
# 5. Medical Copilot 工具执行与多轮槽位解析
# ---------------------------------------------------------------------------

def test_medical_copilot_tool_matcher():
    assert is_medical_query("请帮我算一下bmi，身高175体重70") is True
    assert is_medical_query("华法林和阿司匹林能一起吃吗") is True
    assert is_medical_query("突发剧烈胸痛向后背放射") is True
    assert is_medical_query("帮我查看服务器网络连通性") is False


def test_medical_copilot_execute_emergency_interception():
    service = MedicalService()
    tool = build_medical_copilot_tool(service, "tenant-test-user")
    output = tool.func({"query": "患者突发剧烈压榨性胸痛，向左肩放射满头大汗"})

    data = json.loads(output)
    assert data["status"] == "EMERGENCY_INTERCEPTED"
    assert data["severity"] == "S0"
    assert "120" in data["immediate_action"]


def test_medical_copilot_execute_drug_safety():
    service = MedicalService()
    tool = build_medical_copilot_tool(service, "tenant-test-user")
    output = tool.func({"query": "想请问硝酸甘油和西地那非可以一起服用吗？"})

    data = json.loads(output)
    assert data["status"] == "success"
    assert data["has_s0"] is True
    assert "西地那非" in data["markdown"]


def test_medical_copilot_execute_triage_missing_slots():
    service = MedicalService()
    tool = build_medical_copilot_tool(service, "tenant-test-user")
    # 患者只说了肚子疼，缺失时长、伴随表现和过敏史
    output = tool.func({"query": "医生，我肚子疼，该吃点什么药"})

    data = json.loads(output)
    assert data["status"] == "in_consultation"
    assert data["needs_follow_up"] is True
    assert len(data["missing_slots"]) > 0
    assert "追问" in data["markdown"]


def test_medical_copilot_execute_soap_with_document_writer():
    writer_mock = MagicMock(return_value="doc-uuid-12345")
    service = MedicalService(document_writer=writer_mock)
    tool = build_medical_copilot_tool(service, "tenant-test-user")

    output = tool.func({
        "query": "生成门诊病历",
        "intent": "generate_soap_note",
        "chief_complaint": "转移性右下腹痛3小时",
        "duration": "3小时",
        "accompanying_symptoms": ["恶心", "发热"],
        "allergies": ["头孢过敏"],
        "save_document": True,
    })

    data = json.loads(output)
    assert data["status"] == "success"
    assert data["document_id"] == "doc-uuid-12345"
    assert writer_mock.called
    assert "头孢过敏" in data["markdown"]
