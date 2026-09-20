"""Deterministic medical clinical service, drug safety, and emergency guardrails.

This module provides deterministic calculation engines for clinical scores (BMI,
Cockcroft-Gault eGFR, CHA2DS2-VASc), drug-drug interaction & allergy checking,
S0/S1 emergency red-line detection, and SOAP structured note generation.
Medical metrics and safety gates are strictly deterministic and never delegated
to probabilistic LLMs.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# S0 / S1 急危重症红线规则库（一票否决硬门禁）
# ---------------------------------------------------------------------------

EMERGENCY_RULES = [
    {
        "id": "EMERGENCY_ACS",
        "name": "疑似急性冠脉综合征 / 急性心肌梗死",
        "severity": "S0",
        "keywords": [
            ("胸痛", "左肩"), ("胸痛", "后背"), ("胸痛", "大汗"), ("压榨性", "胸痛"),
            ("压榨感", "胸部"), ("濒死感", "胸"), ("心前区", "压榨"), ("胸骨后", "剧烈疼痛"),
        ],
        "single_keywords": ["急性心梗", "心肌梗死", "心跳骤停"],
        "action": "立刻停止活动并保持安静坐卧位，舌下含服硝酸甘油（若无禁忌），立即拨打 120 急救电话呼叫救护车，切勿自行驾车前往医院！",
    },
    {
        "id": "EMERGENCY_STROKE",
        "name": "疑似急性脑卒中 / 中风（FAST原则）",
        "severity": "S0",
        "keywords": [
            ("口角歪斜", "肢体"), ("言语不清", "一侧肢体"), ("半身不遂",), ("一侧无力", "麻木"),
            ("突发偏瘫",), ("嘴歪", "手麻"), ("吐字不清", "手抬不起"),
        ],
        "single_keywords": ["脑卒中", "脑梗死", "脑出血", "蛛网膜下腔出血"],
        "action": "急性中风救治具有黄金时间窗（发病4.5小时内溶栓），请保持患者平卧头偏向一侧防止误吸，立即拨打 120 送往具备卒中救治资质的医院急诊！",
    },
    {
        "id": "EMERGENCY_ANAPHYLAXIS",
        "name": "疑似严重过敏性休克 / 喉头水肿",
        "severity": "S0",
        "keywords": [
            ("喉头水肿",), ("呼吸骤停",), ("憋气", "全身荨麻疹"), ("过敏", "血压骤降"),
            ("呼吸极其困难", "过敏"), ("窒息感", "皮疹"),
        ],
        "single_keywords": ["过敏性休克"],
        "action": "立即脱离过敏原，保持呼吸道通畅并平卧抬高下肢，立即就地寻找肾上腺素自动注射器（如有），并立即呼叫 120 急救！",
    },
    {
        "id": "EMERGENCY_MASSIVE_HEMORRHAGE",
        "name": "急性严重大出血 / 消化道大出血",
        "severity": "S0",
        "keywords": [
            ("大量呕血",), ("柏油便", "头晕出汗"), ("喷射状出血",), ("咳血不止",),
        ],
        "single_keywords": ["失血性休克"],
        "action": "患者平卧头偏向一侧防误吸，下肢抬高，禁食禁水，外伤活动性出血使用无菌纱布加压包扎，立即呼叫 120 紧急送医！",
    },
    {
        "id": "EMERGENCY_RESPIRATORY_FAILURE",
        "name": "重度急性呼吸困难 / 呼吸衰竭",
        "severity": "S1",
        "keywords": [
            ("端坐呼吸", "发绀"), ("严重喘息", "无法说话"), ("嘴唇发紫", "呼吸困难"),
        ],
        "single_keywords": ["呼吸衰竭", "重症哮喘持续状态"],
        "action": "患者取半坐卧位，保持室内空气流通，尽快吸氧，立即前往附近医院急诊或呼叫 120 急救！",
    },
]


def check_emergency_redline(text: str) -> Optional[Dict[str, Any]]:
    """检测输入中是否触发 S0/S1 急危重症红线规则。

    如命中，返回警报信息；若无则返回 None。
    """
    clean_text = str(text or "").strip()
    if not clean_text:
        return None

    for rule in EMERGENCY_RULES:
        # 单关键词命中
        for sk in rule["single_keywords"]:
            if sk in clean_text:
                return {
                    "triggered": True,
                    "rule_id": rule["id"],
                    "condition_name": rule["name"],
                    "severity": rule["severity"],
                    "action_guide": rule["action"],
                    "matched_pattern": sk,
                }
        # 组合关键词命中
        for kw_group in rule["keywords"]:
            if all(k in clean_text for k in kw_group):
                return {
                    "triggered": True,
                    "rule_id": rule["id"],
                    "condition_name": rule["name"],
                    "severity": rule["severity"],
                    "action_guide": rule["action"],
                    "matched_pattern": "+".join(kw_group),
                }

    return None


# ---------------------------------------------------------------------------
# 药物配伍禁忌与过敏排查矩阵（确定性规则引擎）
# ---------------------------------------------------------------------------

ALLERGY_CLASSES = {
    "青霉素": {
        "class_name": "青霉素类及β-内酰胺类",
        "drugs": [
            "青霉素", "阿莫西林", "氨苄西林", "哌拉西林", "苄星青霉素",
            "阿莫西林克拉维酸钾", "哌拉西林他唑巴坦", "美洛西林",
        ],
    },
    "头孢": {
        "class_name": "头孢菌素类",
        "drugs": [
            "头孢", "头孢克肟", "头孢拉定", "头孢曲松", "头孢唑林",
            "头孢呋辛", "头孢他啶", "头孢吡肟", "头孢地尼",
        ],
    },
    "磺胺": {
        "class_name": "磺胺类",
        "drugs": [
            "磺胺", "复方磺胺甲恶唑", "磺胺嘧啶", "新诺明", "塞来昔布",
        ],
    },
    "阿司匹林/NSAIDs": {
        "class_name": "非甾体抗炎药（NSAIDs）",
        "drugs": [
            "阿司匹林", "布洛芬", "对乙酰氨基酚", "双氯芬酸钠", "吲哚美辛",
            "洛索洛芬钠", "萘普生", "美洛昔康",
        ],
    },
}

DRUG_INTERACTIONS = [
    {
        "id": "DDI_WARFARIN_NSAIDS",
        "drugs": [("华法林",), ("阿司匹林", "布洛芬", "双氯芬酸", "吲哚美辛", "洛索洛芬")],
        "severity": "S1",
        "level": "严重（高出血风险）",
        "mechanism": "NSAIDs 抑制血小板功能并可能损伤胃黏膜，与香豆素类抗凝药华法林联用显著增加消化道大出血风险。",
        "recommendation": "避免合用；若必须止痛退热，建议短期使用对乙酰氨基酚（<2g/日）或局部外用制剂，并严密监测 INR。",
    },
    {
        "id": "DDI_NITRO_SILDENAFIL",
        "drugs": [("硝酸甘油", "单硝酸异山梨酯", "硝酸异山梨酯"), ("西地那非", "他达拉非", "伐地那非")],
        "severity": "S0",
        "level": "绝对禁忌（致命性低血压）",
        "mechanism": "PDE-5 抑制剂协同增强 NO-cGMP 通路，引起全身剧烈血管扩张，合用可诱发不可逆性低血压、休克甚至猝死。",
        "recommendation": "严禁联合使用！服用西地那非后24小时内（他达拉非48小时内）绝对禁止使用任何硝酸酯类药物。",
    },
    {
        "id": "DDI_METFORMIN_CONTRAST",
        "drugs": [("二甲双胍",), ("碘克沙醇", "碘海醇", "碘普罗胺", "含碘造影剂", "造影剂")],
        "severity": "S1",
        "level": "严重（乳酸性酸中毒风险）",
        "mechanism": "静脉注射含碘造影剂可能引发急性肾功能受损，导致二甲双胍在体内蓄积，诱发致死性乳酸性酸中毒。",
        "recommendation": "在进行血管内注射含碘造影剂检查前或检查时须暂停使用二甲双胍，并在检查后至少48小时、复查肾功能正常后方可恢复使用。",
    },
    {
        "id": "DDI_CLOPIDOGREL_OMEPRAZOLE",
        "drugs": [("氯吡格雷",), ("奥美拉唑", "艾司奥美拉唑")],
        "severity": "S2",
        "level": "中度（降低抗血小板疗效）",
        "mechanism": "奥美拉唑强效抑制肝脏 CYP2C19 酶，阻碍氯吡格雷前体活化，削弱其抗血小板聚集疗效，增加支架血栓与心血管事件风险。",
        "recommendation": "建议换用对 CYP2C19 抑制作用较弱的质子泵抑制剂（如泮托拉唑或雷贝拉唑）。",
    },
    {
        "id": "DDI_ACEI_SPIRONOLACTONE",
        "drugs": [
            ("依那普利", "贝那普利", "培哚普利", "福辛普利", "氯沙坦", "缬沙坦", "厄贝沙坦"),
            ("螺内酯", "氨苯蝶啶", "阿米洛利", "保钾利尿剂"),
        ],
        "severity": "S1",
        "level": "严重（高钾血症风险）",
        "mechanism": "ACEI/ARB 抑制醛固酮分泌减少钾排泄，与保钾利尿剂联用可导致严重高钾血症，引起心律失常乃至心跳骤停。",
        "recommendation": "联合使用时须严密监测血清钾及肾功能（定期复查），避免同时补钾。",
    },
    {
        "id": "DDI_STATIN_MACROLIDE",
        "drugs": [("辛伐他汀", "阿托伐他汀", "洛伐他汀"), ("克拉霉素", "红霉素", "阿奇霉素")],
        "severity": "S1",
        "level": "严重（横纹肌溶解风险）",
        "mechanism": "大环内酯类抗生素强效抑制 CYP3A4 代谢酶，导致他汀类药物血药浓度显著升高，诱发肌病及致死性横纹肌溶解症。",
        "recommendation": "使用大环内酯类抗生素期间应暂停辛伐他汀/阿托伐他汀，或换用不经 CYP3A4 代谢的他汀（如瑞舒伐他汀、普伐他汀）。",
    },
]


def check_drug_safety(
    drugs: Sequence[str],
    allergies: Sequence[str] = (),
    patient_conditions: Sequence[str] = (),
) -> Dict[str, Any]:
    """确定性检测用药安全：包含过敏筛查与药物间相互作用 (DDI)。"""
    drug_list = [str(d).strip() for d in drugs if str(d).strip()]
    allergy_list = [str(a).strip() for a in allergies if str(a).strip()]
    condition_list = [str(c).strip() for c in patient_conditions if str(c).strip()]

    conflicts: List[Dict[str, Any]] = []
    allergy_alerts: List[Dict[str, Any]] = []

    # 1. 过敏史筛查
    for allergy in allergy_list:
        allergy_lower = allergy.lower()
        for class_key, class_info in ALLERGY_CLASSES.items():
            if class_key.lower() in allergy_lower or any(d in allergy_lower for d in class_info["drugs"]):
                for drug in drug_list:
                    drug_lower = drug.lower()
                    if any(d.lower() in drug_lower for d in class_info["drugs"]):
                        allergy_alerts.append({
                            "severity": "S0",
                            "level": "绝对禁忌（已知过敏源）",
                            "allergy_declared": allergy,
                            "drug_prescribed": drug,
                            "class_name": class_info["class_name"],
                            "warning": f"患者明确对【{allergy}】过敏，拟使用药物【{drug}】属于该交叉过敏家族，严禁使用！",
                        })

    # 2. 药物相互作用 (DDI) 矩阵排查
    for interaction in DRUG_INTERACTIONS:
        group_a, group_b = interaction["drugs"]
        matched_a = [d for d in drug_list if any(term in d for term in group_a)]
        matched_b = [d for d in drug_list if any(term in d for term in group_b)]
        if matched_a and matched_b:
            conflicts.append({
                "rule_id": interaction["id"],
                "severity": interaction["severity"],
                "level": interaction["level"],
                "drug_pair": f"{matched_a[0]} + {matched_b[0]}",
                "mechanism": interaction["mechanism"],
                "recommendation": interaction["recommendation"],
            })

    # 3. 禁忌疾病校验 (如哮喘禁用非选择性β受体阻滞剂)
    condition_alerts: List[Dict[str, Any]] = []
    for cond in condition_list:
        cond_text = cond.lower()
        if "哮喘" in cond_text or "慢阻肺" in cond_text or "copd" in cond_text:
            for drug in drug_list:
                if any(b in drug for b in ["普萘洛尔", "美托洛尔", "阿替洛尔", "比索洛尔"]):
                    condition_alerts.append({
                        "severity": "S1",
                        "condition": cond,
                        "drug": drug,
                        "warning": f"患者患有【{cond}】，使用β受体阻滞剂【{drug}】可能诱发严重支气管痉挛！",
                    })

    is_safe = (len(allergy_alerts) == 0) and (len(conflicts) == 0) and (len(condition_alerts) == 0)
    has_s0 = any(a.get("severity") == "S0" for a in allergy_alerts) or any(c.get("severity") == "S0" for c in conflicts)

    return {
        "is_safe": is_safe,
        "has_s0_contraindication": has_s0,
        "drugs_checked": drug_list,
        "allergies_declared": allergy_list,
        "allergy_alerts": allergy_alerts,
        "interaction_conflicts": conflicts,
        "condition_alerts": condition_alerts,
        "summary": (
            "未发现明显药物配伍禁忌与过敏冲突"
            if is_safe
            else f"检测到 {len(allergy_alerts)} 项过敏告警，{len(conflicts)} 项相互作用风险，{len(condition_alerts)} 项疾病禁忌！"
        ),
    }


# ---------------------------------------------------------------------------
# 确定性临床医学计算引擎（BMI / GFR / CHA2DS2-VASc）
# ---------------------------------------------------------------------------

def calculate_bmi(height_cm: float, weight_kg: float) -> Dict[str, Any]:
    """计算体质指数 (BMI)，依据中国与WHO成人标准。"""
    if height_cm <= 0 or weight_kg <= 0:
        raise ValueError("身高与体重必须为大于0的数值")
    if height_cm > 300 or weight_kg > 500:
        raise ValueError("身高或体重数值超出人体生理可能范围")

    height_m = height_cm / 100.0
    bmi = round(weight_kg / (height_m * height_m), 2)

    # 中国成人 BMI 标准 (WS/T 428-2013)
    if bmi < 18.5:
        category = "体重过轻 (Underweight)"
        recommendation = "建议加强均衡营养摄入，排查甲亢、吸收不良或慢性消耗性疾病。"
    elif 18.5 <= bmi < 24.0:
        category = "正常体重 (Normal)"
        recommendation = "指标优良，请继续保持健康饮食与规律运动习惯。"
    elif 24.0 <= bmi < 28.0:
        category = "超重 (Overweight)"
        recommendation = "提示超重，建议控制碳水及油脂摄入，每周进行150分钟中等强度有氧运动。"
    else:
        category = "肥胖 (Obese)"
        recommendation = "提示肥胖，显著增加高血压、2型糖尿病及心血管疾病风险，建议临床内分泌/营养科随诊指导减重。"

    return {
        "metric": "BMI",
        "height_cm": height_cm,
        "weight_kg": weight_kg,
        "value": bmi,
        "category": category,
        "reference_range": "18.5 - 23.9 kg/m²",
        "recommendation": recommendation,
    }


def calculate_gfr(
    age: int,
    weight_kg: float,
    serum_creatinine_umol_l: float,
    sex: str = "male",
) -> Dict[str, Any]:
    """计算内生肌酐清除率 / 估算肾小球滤过率 (Cockcroft-Gault 公式)。

    公式：
    Ccr = ((140 - age) * weight_kg) / (0.818 * Scr(umol/L))
    女性乘以 0.85
    """
    if age < 18 or age > 120:
        raise ValueError("Cockcroft-Gault 公式适用于 18 岁以上成人")
    if weight_kg <= 20 or weight_kg > 300:
        raise ValueError("体重数值不符合成人常规范围")
    if serum_creatinine_umol_l <= 10 or serum_creatinine_umol_l > 2500:
        raise ValueError("血清肌酐必须为合理的生化检验数值 (umol/L)")

    is_female = str(sex or "").strip().lower() in {"female", "f", "女", "女性"}
    cr_clearance = ((140.0 - age) * weight_kg) / (0.818 * serum_creatinine_umol_l)
    if is_female:
        cr_clearance *= 0.85
    cr_clearance = round(cr_clearance, 2)

    # CKD 分期
    if cr_clearance >= 90:
        stage = "CKD 1期（肾功能正常或代偿期）"
        dose_advice = "常规药物一般无需因肾功能调整剂量。"
    elif 60 <= cr_clearance < 90:
        stage = "CKD 2期（轻度肾功能受损）"
        dose_advice = "大部分经肾排泄药物可全量使用，部分狭窄治疗窗药物需注意监测。"
    elif 30 <= cr_clearance < 60:
        stage = "CKD 3期（中度肾功能受损）"
        dose_advice = "注意！抗生素（如氨基糖苷类、头孢菌素）、降糖药（二甲双胍慎用/减量）等须依据说明书调整给药间隔或剂量。"
    elif 15 <= cr_clearance < 30:
        stage = "CKD 4期（重度肾功能受损）"
        dose_advice = "高危警告：禁用二甲双胍、NSAIDs；严禁使用肾毒性药物；大部分抗生素需减量50%以上或由肾内科专科评估。"
    else:
        stage = "CKD 5期（终末期肾病 / 肾衰竭）"
        dose_advice = "严重警告：进入尿毒症期，须肾脏替代治疗（血液透析/腹膜透析），严禁随意给药。"

    return {
        "metric": "eGFR_Cockcroft_Gault",
        "age": age,
        "weight_kg": weight_kg,
        "sex": "女性" if is_female else "男性",
        "serum_creatinine_umol_l": serum_creatinine_umol_l,
        "value": cr_clearance,
        "unit": "ml/min",
        "stage": stage,
        "clinical_advice": dose_advice,
    }


def calculate_cha2ds2_vasc(
    age: int,
    sex: str = "male",
    congestive_heart_failure: bool = False,
    hypertension: bool = False,
    diabetes: bool = False,
    stroke_or_tia: bool = False,
    vascular_disease: bool = False,
) -> Dict[str, Any]:
    """计算非瓣膜性心房颤动患者 CHA2DS2-VASc 卒中风险评分。"""
    score = 0
    breakdown: List[str] = []

    # C: 心衰 (+1)
    if congestive_heart_failure:
        score += 1
        breakdown.append("充血性心力衰竭/左室功能障碍 (+1分)")

    # H: 高血压 (+1)
    if hypertension:
        score += 1
        breakdown.append("高血压病史 (+1分)")

    # A2: 年龄 >= 75 (+2), 65-74 (+1)
    if age >= 75:
        score += 2
        breakdown.append(f"年龄 {age} 岁 (>=75岁, +2分)")
    elif 65 <= age <= 74:
        score += 1
        breakdown.append(f"年龄 {age} 岁 (65-74岁, +1分)")

    # D: 糖尿病 (+1)
    if diabetes:
        score += 1
        breakdown.append("糖尿病史 (+1分)")

    # S2: 既往卒中/TIA/血栓栓塞 (+2)
    if stroke_or_tia:
        score += 2
        breakdown.append("既往卒中 / TIA / 动脉血栓栓塞病史 (+2分)")

    # V: 血管疾病 (+1)
    if vascular_disease:
        score += 1
        breakdown.append("血管疾病（心梗/外周动脉疾病/主动脉斑块, +1分)")

    # Sc: 女性 (+1)
    is_female = str(sex or "").strip().lower() in {"female", "f", "女", "女性"}
    if is_female:
        score += 1
        breakdown.append("女性性别特征 (+1分)")

    risk_table = {
        0: 0.2, 1: 0.6, 2: 2.2, 3: 3.2, 4: 4.8, 5: 7.2, 6: 9.7, 7: 11.2, 8: 10.8, 9: 12.2,
    }
    annual_risk = risk_table.get(min(score, 9), 12.2)

    threshold_need = 3 if is_female else 2
    threshold_consider = 2 if is_female else 1

    if score >= threshold_need:
        recommendation = "【强烈推荐口服抗凝药 (OAC)】卒中高危人群，推荐使用非维生素K拮抗剂口服抗凝药 (NOAC，如阿哌沙班、利伐沙班) 或华法林。"
    elif score >= threshold_consider:
        recommendation = "【可考虑口服抗凝药 (OAC)】存在卒中中度风险，权衡 HAS-BLED 出血风险后，推荐个体化启用抗凝治疗。"
    else:
        recommendation = "【无需抗凝或抗血小板治疗】卒中低危人群，不建议常规抗凝，定期动态复评卒中危险因素。"

    return {
        "metric": "CHA2DS2-VASc",
        "score": score,
        "max_score": 9,
        "breakdown": breakdown,
        "estimated_annual_stroke_rate": f"{annual_risk}%",
        "anticoagulation_recommendation": recommendation,
    }


# ---------------------------------------------------------------------------
# 结构化门诊 SOAP 电子病历生成
# ---------------------------------------------------------------------------

def generate_soap_note(
    patient_info: Dict[str, Any],
    subjective: Dict[str, Any],
    objective: Dict[str, Any],
    assessment: Dict[str, Any],
    plan: Dict[str, Any],
    evidence_ids: Sequence[str] = (),
) -> str:
    """生成符合国家病历书写规范的门诊 SOAP 结构化 Markdown 病历。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    name = patient_info.get("name") or "患者"
    gender = patient_info.get("gender") or patient_info.get("sex") or "未注"
    age = patient_info.get("age") or "未注"
    case_no = patient_info.get("case_no") or "EMR-" + datetime.now().strftime("%Y%m%d%H%M")

    # Subjective 主诉与病史
    chief_complaint = subjective.get("chief_complaint") or "未记录"
    hpi = subjective.get("history_of_present_illness") or "未记录"
    past_history = subjective.get("past_history") or "无特殊"
    allergies = subjective.get("allergies") or "未诉药物过敏史"

    # Objective 客观检查
    vitals = objective.get("vitals") or {}
    physical_exam = objective.get("physical_exam") or "未查"
    labs = objective.get("labs") or []

    vitals_str = ", ".join(f"{k}: {v}" for k, v in vitals.items()) if vitals else "血压、脉搏等生命体征平稳"
    labs_str = "\n".join(f"- {item}" for item in labs) if labs else "暂无辅助检查记录"

    # Assessment 诊断与评估
    primary_diag = assessment.get("primary_diagnosis") or "待查"
    diff_diag = assessment.get("differential_diagnosis") or []
    diff_str = ", ".join(diff_diag) if diff_diag else "待排查相关疾病"

    # Plan 处置计划
    treatment = plan.get("treatment") or []
    rx_str = "\n".join(f"- {item}" for item in treatment) if treatment else "- 遵医嘱生活方式干预"
    precautions = plan.get("precautions") or "若症状加重或出现突发不适，请立即就近就医。"

    evidence_sec = ""
    if evidence_ids:
        evidence_sec = f"\n\n**循证医学支持证据引用 (Evidence IDs)**:\n" + "\n".join(f"- [{eid}]" for eid in evidence_ids)

    return f"""# 门诊结构化医疗病历记录 (SOAP Note)

- **病历编号**: `{case_no}`
- **就诊时间**: {now_str}
- **患者基本信息**: {name} | 性别: {gender} | 年龄: {age}岁
- **药物过敏史**: {allergies}

---

### S - Subjective（主诉与病史）
- **主诉**: {chief_complaint}
- **现病史**: {hpi}
- **既往史**: {past_history}

### O - Objective（客观体征与检查）
- **生命体征**: {vitals_str}
- **查体所见**: {physical_exam}
- **检验检查结果**:
{labs_str}

### A - Assessment（临床评估与诊断）
- **初步拟诊**: **{primary_diag}**
- **鉴别诊断**: {diff_str}

### P - Plan（诊疗与随访计划）
- **拟定方案 / 处方医嘱**:
{rx_str}
- **健康宣教与注意事项**: {precautions}{evidence_sec}

---
*免责声明：本记录由 AI 临床辅助决策系统（Medical Copilot）生成，仅供注册执业医师诊疗参考，不可直接作为正式处方使用。*
"""


class MedicalService:
    """领域业务门面：集中管理医学指标计算、安全审查与病历生成。"""

    def __init__(self, document_writer: Optional[Any] = None):
        self.document_writer = document_writer

    def check_emergency(self, query: str) -> Optional[Dict[str, Any]]:
        return check_emergency_redline(query)

    def check_safety(
        self,
        drugs: Sequence[str],
        allergies: Sequence[str] = (),
        conditions: Sequence[str] = (),
    ) -> Dict[str, Any]:
        return check_drug_safety(drugs, allergies, conditions)

    def calculate_score(self, score_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        def _require(key: str, cast):
            """缺失或非法参数统一抛 ValueError，HTTP 层映射为 400 而非 500。"""
            value = params.get(key)
            if value is None or str(value).strip() == "":
                raise ValueError(f"缺少必需参数: {key}")
            try:
                return cast(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"参数 {key} 的值无效: {value!r}") from exc

        norm_type = str(score_type or "").strip().lower()
        if "bmi" in norm_type or "体质指数" in norm_type:
            return calculate_bmi(_require("height_cm", float), _require("weight_kg", float))
        elif "gfr" in norm_type or "肌酐" in norm_type or "肾" in norm_type:
            return calculate_gfr(
                age=_require("age", int),
                weight_kg=_require("weight_kg", float),
                serum_creatinine_umol_l=_require("serum_creatinine_umol_l", float),
                sex=str(params.get("sex", "male")),
            )
        elif "cha2ds2" in norm_type or "vasc" in norm_type or "房颤" in norm_type or "卒中" in norm_type:
            return calculate_cha2ds2_vasc(
                age=_require("age", int),
                sex=str(params.get("sex", "male")),
                congestive_heart_failure=bool(params.get("chf", False)),
                hypertension=bool(params.get("hypertension", False)),
                diabetes=bool(params.get("diabetes", False)),
                stroke_or_tia=bool(params.get("stroke_or_tia", False)),
                vascular_disease=bool(params.get("vascular_disease", False)),
            )
        else:
            raise ValueError(f"不支持的医学计算类型: {score_type}，可选: bmi, gfr, cha2ds2_vasc")

    def build_soap_record(
        self,
        patient_info: Dict[str, Any],
        subjective: Dict[str, Any],
        objective: Dict[str, Any],
        assessment: Dict[str, Any],
        plan: Dict[str, Any],
        evidence_ids: Sequence[str] = (),
    ) -> str:
        return generate_soap_note(patient_info, subjective, objective, assessment, plan, evidence_ids)
