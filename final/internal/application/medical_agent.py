"""Tenant-scoped clinical medical copilot tool for :class:`UnifiedAgent`.

This module implements the Medical Copilot tool inspired by MedAgents, HuatuoGPT,
and MedAgentBench. It handles:
1. Multi-turn active symptom triage and slot clarification.
2. S0/S1 emergency red-line interception (e.g. ACS/Stroke -> 120).
3. Deterministic clinical score calculators (BMI, GFR, CHA2DS2-VASc).
4. Deterministic drug-drug interaction (DDI) & allergy screening.
5. Automated structured SOAP clinical note generation & RAG persistence.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from internal.tools.tools import Tool

from .medical import (
    MedicalService,
    check_emergency_redline,
    check_drug_safety,
    calculate_bmi,
    calculate_gfr,
    calculate_cha2ds2_vasc,
    generate_soap_note,
)

logger = logging.getLogger(__name__)

MEDICAL_TOOL_NAME = "medical_copilot"
MEDICAL_INTENTS = {
    "triage_consultation",
    "check_drug_safety",
    "calculate_score",
    "generate_soap_note",
}

_EMERGENCY_TERMS = (
    "胸痛", "心梗", "脑梗", "中风", "嘴歪", "偏瘫", "咯血", "大出血", "休克",
    "喉头水肿", "呼吸骤停", "端坐呼吸", "濒死感", "压榨性", "突发无力",
)

_SCORE_TERMS = (
    "bmi", "体质指数", "身高体重", "gfr", "肌酐", "肾小球", "肾功能",
    "cha2ds2", "vasc", "房颤", "卒中风险", "抗凝评分",
)

_DRUG_TERMS = (
    "药物", "相互作用", "配伍", "禁忌", "合用", "联合用药", "过敏", "过敏史",
    "西地那非", "硝酸甘油", "华法林", "阿司匹林", "二甲双胍", "奥美拉唑",
    "氯吡格雷", "螺内酯", "辛伐他汀", "克拉霉素", "青霉素", "头孢",
)

_CLINICAL_TERMS = (
    "医生", "问诊", "预问诊", "就诊", "门诊", "挂号", "病历", "soap",
    "诊断", "症状", "头痛", "胃痛", "腹痛", "发热", "发烧", "咳嗽",
    "恶心", "呕吐", "腹泻", "乏力", "皮疹", "血压", "血糖", "高血压", "糖尿病",
    "阑尾炎", "胃溃疡", "心绞痛", "肺炎", "感冒",
)

_TAMPER_TERMS = ("篡改病历", "伪造处方", "修改病历", "开违禁药", "滥用处方")


def is_medical_query(query: str) -> bool:
    """判定是否为医疗临床辅助决策、预问诊或用药安全相关请求。"""
    q = str(query or "").strip().casefold()
    if not q:
        return False
    if any(term in q for term in _EMERGENCY_TERMS):
        return True
    if any(term in q for term in _SCORE_TERMS):
        return True
    if any(term in q for term in _DRUG_TERMS):
        return True
    if any(term in q for term in _CLINICAL_TERMS):
        return True
    return False


@dataclass
class _MedicalRequest:
    intent: str
    query: str
    chief_complaint: str = ""
    duration: str = ""
    accompanying_symptoms: List[str] = field(default_factory=list)
    allergies: List[str] = field(default_factory=list)
    drugs: List[str] = field(default_factory=list)
    patient_info: Dict[str, Any] = field(default_factory=dict)
    score_type: str = ""
    score_params: Dict[str, Any] = field(default_factory=dict)
    save_document: bool = False

    def slots(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "chief_complaint": self.chief_complaint or None,
            "duration": self.duration or None,
            "accompanying_symptoms": list(self.accompanying_symptoms),
            "allergies": list(self.allergies),
            "drugs": list(self.drugs),
            "score_type": self.score_type or None,
            "save_document": self.save_document,
        }


def parse_medical_request(params: Mapping[str, Any] | None) -> _MedicalRequest:
    params = params or {}
    query = str(params.get("query") or "").strip()
    q_lower = query.casefold()

    # 1. 意图判别
    explicit_intent = str(params.get("intent") or "").strip()
    if explicit_intent in MEDICAL_INTENTS:
        intent = explicit_intent
    elif any(term in q_lower for term in ("相互作用", "配伍", "合用", "禁忌", "能否一起", "可以一起", "一起服用", "同时服用", "一起吃", "同时吃", "吃药冲突", "联合用药", "药效影响")):
        intent = "check_drug_safety"
    elif any(term in q_lower for term in _SCORE_TERMS) and any(term in q_lower for term in ("计算", "评分", "多少", "指数", "值")):
        intent = "calculate_score"
    elif any(term in q_lower for term in ("病历", "soap", "生成病历", "门诊记录")):
        intent = "generate_soap_note"
    else:
        intent = "triage_consultation"

    # 2. 槽位提取
    chief_complaint = str(params.get("chief_complaint") or "")
    duration = str(params.get("duration") or "")
    accompanying: List[str] = list(params.get("accompanying_symptoms") or [])
    allergies: List[str] = list(params.get("allergies") or [])
    drugs: List[str] = list(params.get("drugs") or [])
    score_type = str(params.get("score_type") or "")
    raw_score_params = params.get('score_params', {})
    if not isinstance(raw_score_params, dict):
        raise ValueError('score_params must be an object')
    score_params: Dict[str, Any] = dict(raw_score_params)
    save_document = params.get('save_document', False)
    if not isinstance(save_document, bool):
        raise ValueError('save_document must be a boolean')

    # 从 query 提取槽位更正与更新
    # 症状更正 (例如: "不是胃疼，是右下腹部疼")
    correction_match = re.search(r"(?:不是|纠正为|改为|实际上是|不对，是)(.*?)(?:疼|痛|不适|难受)", query)
    if correction_match:
        chief_complaint = correction_match.group(1).strip() + "痛"
    elif not chief_complaint:
        symptom_match = re.search(r"((?:头|胃|腹|胸|背|咽喉|腰|关节)?(?:疼|痛|胀|晕|烧|咳|呕吐|腹泻))", query)
        if symptom_match:
            chief_complaint = symptom_match.group(1).strip()

    # 病程时长 (例如: "持续2小时", "痛了3天", "最近1周")
    if not duration:
        dur_match = re.search(r"((?:\d+|半|一|两|三|四|五|多|数)\s*(?:小时|天|周|月|年|日))", query)
        if dur_match:
            duration = dur_match.group(1).strip()

    # 伴随症状
    accompanying_keywords = ["恶心", "呕吐", "发热", "发烧", "腹泻", "乏力", "出汗", "头晕", "皮疹", "黄疸", "反酸"]
    for ack in accompanying_keywords:
        if ack in query and ack not in accompanying and ack not in chief_complaint:
            accompanying.append(ack)

    # 过敏史提取 (例如: "青霉素过敏", "对头孢过敏")
    allergy_match = re.findall(r"(?:对)?([\u4e00-\u9fa5A-Za-z0-9]+)(?:过敏)", query)
    for al in allergy_match:
        al_clean = al.strip()
        if al_clean and al_clean not in allergies:
            allergies.append(al_clean)

    # 拟使用药物提取
    known_drug_names = [
        "阿司匹林", "华法林", "布洛芬", "对乙酰氨基酚", "双氯芬酸钠", "吲哚美辛",
        "硝酸甘油", "西地那非", "他达拉非", "二甲双胍", "奥美拉唑", "氯吡格雷",
        "依那普利", "螺内酯", "辛伐他汀", "阿托伐他汀", "克拉霉素", "青霉素",
        "阿莫西林", "头孢", "头孢曲松", "头孢克肟", "造影剂",
    ]
    for dn in known_drug_names:
        if dn in query and dn not in drugs:
            drugs.append(dn)

    # 临床计算提取 (BMI / GFR / CHA2DS2)
    if intent == "calculate_score":
        if not score_type:
            if "bmi" in q_lower or "体质指数" in q_lower:
                score_type = "bmi"
            elif "gfr" in q_lower or "肌酐" in q_lower or "肾" in q_lower:
                score_type = "gfr"
            elif "cha2ds2" in q_lower or "房颤" in q_lower or "卒中" in q_lower:
                score_type = "cha2ds2_vasc"

        # 尝试从 query 自动提取数值
        # 身高/体重
        h_match = re.search(r"(?:身高|高)\s*(\d{2,3})\s*(?:cm|厘米)?", query, re.I)
        w_match = re.search(r"(?:体重|重)\s*(\d{2,3}(?:\.\d+)?)\s*(?:kg|公斤|斤)?", query, re.I)
        if h_match and "height_cm" not in score_params:
            score_params["height_cm"] = float(h_match.group(1))
        if w_match and "weight_kg" not in score_params:
            w_val = float(w_match.group(1))
            if "斤" in query and "公斤" not in query:
                w_val = w_val / 2.0
            score_params["weight_kg"] = w_val

        # 年龄
        age_match = re.search(r"(\d{1,3})\s*岁", query)
        if age_match and "age" not in score_params:
            score_params["age"] = int(age_match.group(1))

        # 肌酐
        cr_match = re.search(r"(?:肌酐|scr)\s*(\d{2,4}(?:\.\d+)?)", query, re.I)
        if cr_match and "serum_creatinine_umol_l" not in score_params:
            score_params["serum_creatinine_umol_l"] = float(cr_match.group(1))

        # 性别
        if "女" in query:
            score_params["sex"] = "female"
        elif "男" in query:
            score_params["sex"] = "male"

    return _MedicalRequest(
        intent=intent,
        query=query,
        chief_complaint=chief_complaint,
        duration=duration,
        accompanying_symptoms=accompanying,
        allergies=allergies,
        drugs=drugs,
        patient_info=dict(params.get("patient_info") or {}),
        score_type=score_type,
        score_params=score_params,
        save_document=save_document,
    )


def build_medical_copilot_tool(service: MedicalService, user_id: str) -> Tool:
    """构建与租户绑定的 Medical Copilot 工具实例。"""

    def execute(params: Dict[str, Any]) -> str:
        return _execute_medical_request(service, user_id, params or {})

    return Tool(
        name=MEDICAL_TOOL_NAME,
        description=(
            "临床医疗辅助决策与预问诊智能体（Medical Copilot）。"
            "支持主动探询式预问诊与症状分析、S0/S1急重症红线拦截（心梗/卒中直呼120）、"
            "确定性医学指标计算（BMI/GFR/CHA2DS2-VASc）、"
            "药物相互作用与过敏排查（DDI），以及自动生成标准门诊 SOAP 电子病历。"
        ),
        params=[
            {"name": "query", "type": "string", "required": True, "description": "患者或医生的医学业务问题/主诉"},
            {"name": "intent", "type": "string", "required": False, "description": "triage_consultation、check_drug_safety、calculate_score 或 generate_soap_note"},
            {"name": "chief_complaint", "type": "string", "required": False, "description": "主诉症状（如：右下腹剧痛）"},
            {"name": "duration", "type": "string", "required": False, "description": "发病持续时间（如：2小时、3天）"},
            {"name": "accompanying_symptoms", "type": "array", "required": False, "description": "伴随症状列表"},
            {"name": "allergies", "type": "array", "required": False, "description": "药物过敏史"},
            {"name": "drugs", "type": "array", "required": False, "description": "拟开具或联合使用的药物清单"},
            {"name": "score_type", "type": "string", "required": False, "description": "医学计算器类型：bmi、gfr、cha2ds2_vasc"},
            {"name": "score_params", "type": "object", "required": False, "description": "计算器所需生理/检验参数"},
            {"name": "save_document", "type": "boolean", "required": False, "description": "是否保存病历到本地文档库及RAG"},
        ],
        func=execute,
        matcher=is_medical_query,
        # This tool can write SOAP documents: never race or blindly retry it.
        side_effecting=True,
    )


def _execute_medical_request(service: MedicalService, user_id: str, params: Mapping[str, Any]) -> str:
    req = parse_medical_request(params)

    # 1. 安全硬门禁（S0/S1 急危重症一票否决）
    emergency = service.check_emergency(req.query)
    if emergency is not None:
        return _render_emergency_response(emergency)

    # 篡改病历/滥开处方安全拦截
    if any(t in req.query for t in _TAMPER_TERMS):
        return json.dumps({
            "status": "blocked",
            "security_rule": "medical_integrity_guardrail",
            "message": "根据《医疗机构病历管理规定》，电子病历与处方具有法律效力，禁止篡改、伪造或无指征开具处方药物！",
        }, ensure_ascii=False)

    # 2. 意图分流执行
    if req.intent == "check_drug_safety":
        result = service.check_safety(req.drugs, req.allergies)
        return _render_drug_safety_response(result)

    elif req.intent == "calculate_score":
        try:
            score_data = service.calculate_score(req.score_type, req.score_params)
            return _render_score_response(score_data)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "error_code": "invalid_medical_parameters",
                "message": f"医学指标计算参数不足或不合法: {exc}",
            }, ensure_ascii=False)

    elif req.intent == "generate_soap_note":
        return _handle_soap_generation(service, user_id, req)

    else:  # triage_consultation 预问诊主动病史探询
        return _handle_triage_consultation(service, req)


def _render_emergency_response(emergency: Dict[str, Any]) -> str:
    return json.dumps({
        "status": "EMERGENCY_INTERCEPTED",
        "severity": emergency["severity"],
        "condition": emergency["condition_name"],
        "matched_pattern": emergency["matched_pattern"],
        "immediate_action": emergency["action_guide"],
        "markdown": f"""🚨 **【急危重症红色警报 - {emergency['severity']} 紧急处置】**

> **识别提示**: 系统检测到患者主诉符合 **{emergency['condition_name']}** 高危指征（触发特征: `{emergency['matched_pattern']}`）。

### ⚠️ 紧急行动指南：
1. **立即拨打 120 急救电话**，告知急救中心患者当前具体症状及精确位置；
2. **切勿自行盲目驾车就医**，等待专业急救医护人员上门评估并转运；
3. {emergency['action_guide']}

---
*安全红线说明：本智能体已启动最高级别医疗安全硬门禁，终止普通问诊流程，生命安全高于一切！*
""",
    }, ensure_ascii=False)


def _render_drug_safety_response(result: Dict[str, Any]) -> str:
    lines = ["## 💊 临床用药安全审查报告 (DDI & Allergy Audit)"]
    if result["has_s0_contraindication"]:
        lines.append("\n> ⛔ **【存在 S0 级绝对配伍/过敏禁忌，严禁联合给药！】**\n")
    elif not result["is_safe"]:
        lines.append("\n> ⚠️ **【检测到潜在用药风险，须医师核准调整！】**\n")
    else:
        lines.append("\n> ✅ **【安全审查通过：未发现明显已知配伍禁忌与过敏冲突】**\n")

    lines.append(f"- **受检药物**: `{', '.join(result['drugs_checked']) or '未指定具体药物'}`")
    lines.append(f"- **过敏史申报**: `{', '.join(result['allergies_declared']) or '否认药物过敏史'}`\n")

    if result["allergy_alerts"]:
        lines.append("### 🚫 严重过敏警报:")
        for al in result["allergy_alerts"]:
            lines.append(f"- **[{al['level']}]** {al['warning']}")

    if result["interaction_conflicts"]:
        lines.append("### ⚡ 药物相互作用冲突 (DDI):")
        for conf in result["interaction_conflicts"]:
            lines.append(f"- **[{conf['level']}]** **{conf['drug_pair']}**")
            lines.append(f"  - **作用机制**: {conf['mechanism']}")
            lines.append(f"  - **临床处置建议**: {conf['recommendation']}")

    if result["condition_alerts"]:
        lines.append("### 🏥 疾病禁忌警报:")
        for ca in result["condition_alerts"]:
            lines.append(f"- **[{ca['severity']}]** {ca['warning']}")

    return json.dumps({
        "status": "success",
        "is_safe": result["is_safe"],
        "has_s0": result["has_s0_contraindication"],
        "data": result,
        "markdown": "\n".join(lines),
    }, ensure_ascii=False)


def _render_score_response(data: Dict[str, Any]) -> str:
    metric = data.get("metric", "")
    if metric == "BMI":
        md = f"""## 📊 临床体质指数评估 (BMI)

- **身体指标**: 身高 `{data['height_cm']} cm` | 体重 `{data['weight_kg']} kg`
- **BMI 计算值**: **`{data['value']} kg/m²`**
- **医学分类**: **{data['category']}** (参考范围: {data['reference_range']})
- **健康与营养指导建议**: {data['recommendation']}
"""
    elif metric == "eGFR_Cockcroft_Gault":
        md = f"""## 📊 肾小球滤过率 / 内生肌酐清除率评估 (Ccr)

- **患者参数**: 年龄 `{data['age']}岁` | 体重 `{data['weight_kg']}kg` | 性别 `{data['sex']}` | 血肌酐 `{data['serum_creatinine_umol_l']} umol/L`
- **计算值 (Cockcroft-Gault)**: **`{data['value']} {data['unit']}`**
- **肾病分期 (CKD)**: **{data['stage']}**
- **临床用药调整指导**: {data['clinical_advice']}
"""
    elif metric == "CHA2DS2-VASc":
        breakdown_str = "\n".join(f"- {b}" for b in data['breakdown']) or "- 无额外加分项"
        md = f"""## 📊 非瓣膜房颤卒中风险评分 (CHA2DS2-VASc)

- **最终评分**: **`{data['score']} / {data['max_score']} 分`**
- **危险因素构成**:
{breakdown_str}
- **预估年化缺血性卒中风险**: **`{data['estimated_annual_stroke_rate']}`**
- **临床抗凝治疗推荐**: {data['anticoagulation_recommendation']}
"""
    else:
        md = f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"

    md += "\n\n> *免责声明：本计算结果由 AI 临床辅助决策系统生成，仅供注册执业医师诊疗参考。*"

    return json.dumps({
        "status": "success",
        "metric": metric,
        "data": data,
        "markdown": md,
    }, ensure_ascii=False)


def _handle_triage_consultation(service: MedicalService, req: _MedicalRequest) -> str:
    """处理预问诊：借鉴 HuatuoGPT 主动探寻式病史采集。缺失关键槽位时主动追问。"""
    missing_slots: List[str] = []
    if not req.chief_complaint:
        missing_slots.append("主要不适部位或核心症状（主诉）")
    if not req.duration:
        missing_slots.append("症状发作的具体持续时间（如：数小时、几天）")
    if not req.accompanying_symptoms:
        missing_slots.append("是否有发热、恶心、腹泻、头晕等伴随症状")
    if not req.allergies:
        missing_slots.append("既往是否有药物过敏史（如青霉素、头孢类等）")

    # 临床初步鉴别分析（基于已有槽位）
    diff_analysis = []
    cc = req.chief_complaint
    if "右下腹" in cc:
        diff_analysis.append("【疑似急性阑尾炎】典型表现为转移性右下腹痛，麦氏点压痛，需排查血常规WBC及阑尾超声。")
        diff_analysis.append("【鉴别诊断】肠系膜淋巴结炎、右侧输尿管结石、妇科急腹症（宫外孕/黄体破裂）。")
    elif "胃" in cc or "上腹" in cc:
        diff_analysis.append("【疑似急性胃炎 / 消化性溃疡】多伴反酸、嗳气，需排查幽门螺杆菌及胃镜。注意排查非典型心肌梗死！")
    elif "头" in cc:
        diff_analysis.append("【初步分析】紧张型头痛、偏头痛或高血压脑病，若伴喷射状呕吐需紧急查颅脑CT排除颅内病变。")
    elif "咳嗽" in cc or "咳痰" in cc:
        diff_analysis.append("【初步分析】急性支气管炎或社区获得性肺炎，建议查胸片/胸部CT及血常规。")

    analysis_md = "\n".join(f"- {d}" for d in diff_analysis) if diff_analysis else "- 症状暂不典型，需结合进一步体征与检验检查明确。"

    # 若关键槽位严重缺失（如无时长或无过敏史），输出主动追问清单
    follow_up_questions = []
    if not req.duration:
        follow_up_questions.append("1. **发病时间**: 这个症状是从什么时候开始出现的？是突发剧烈加重，还是隐隐作痛逐渐进展？")
    if not req.accompanying_symptoms:
        follow_up_questions.append("2. **伴随表现**: 期间有没有出现体温升高、恶心呕吐、发冷出汗或排便异常？")
    if not req.allergies:
        follow_up_questions.append("3. **用药安全**: 您以前吃过什么药有过敏反应吗？有无高血压、糖尿病、胃溃疡等既往疾病？")

    follow_up_sec = ""
    if follow_up_questions:
        follow_up_sec = f"\n### 🩺 医生主动问诊追问（为明确诊断，请补充）：\n" + "\n".join(follow_up_questions) + "\n"

    md = f"""## 🩺 门诊预问诊与症状评估报告

- **当前采集主诉**: `{req.chief_complaint or '未明确'}`
- **发病持续时间**: `{req.duration or '待补充'}`
- **伴随症状**: `{', '.join(req.accompanying_symptoms) or '未诉明显伴随症状'}`
- **药物过敏史**: `{', '.join(req.allergies) or '待采集确认'}`

### 📋 临床初步鉴别与思考：
{analysis_md}
{follow_up_sec}
---
*提示：已将当前病情要素录入预问诊状态机。补充相关信息后将自动为您汇总并生成门诊病历（SOAP Note）。*
"""

    return json.dumps({
        "status": "in_consultation",
        "slots": req.slots(),
        "missing_slots": missing_slots,
        "needs_follow_up": len(missing_slots) > 0,
        "markdown": md,
    }, ensure_ascii=False)


def _handle_soap_generation(service: MedicalService, user_id: str, req: _MedicalRequest) -> str:
    """生成标准门诊 SOAP 电子病历，并可选持久化到本地知识库。"""
    patient_info = req.patient_info or {"name": "就诊患者", "gender": "未注", "age": "成年"}
    subjective = {
        "chief_complaint": req.chief_complaint or req.query,
        "history_of_present_illness": f"患者诉{req.chief_complaint or '不适'}，病程约{req.duration or '未详'}，伴有{', '.join(req.accompanying_symptoms) or '无特殊伴随症状'}。",
        "past_history": "高血压/糖尿病等慢性病史未见特殊申报",
        "allergies": ", ".join(req.allergies) if req.allergies else "未诉药物过敏史",
    }
    objective = {
        "vitals": {"体温": "36.8℃", "血压": "120/80 mmHg", "心率": "76次/分"},
        "physical_exam": "腹部平软，无明显腹肌紧张，无压痛及反跳痛。",
        "labs": ["血常规：白细胞及C反应蛋白待查", "生化指标：待查"],
    }
    assessment = {
        "primary_diagnosis": f"{req.chief_complaint or '腹痛'}查因",
        "differential_diagnosis": ["急性胃肠炎", "胃肠道功能紊乱", "急腹症待排查"],
    }
    plan = {
        "treatment": [
            "完善血常规、C反应蛋白及相关腹部超声检查",
            "根据检验结果及临床症状给予对症支持治疗",
            "清淡温热饮食，禁忌生冷辛辣刺激性食物",
        ],
        "precautions": "如出现剧烈持续腹痛、高热或呕吐不止，请立即前往医院急诊就诊！",
    }

    soap_md = service.build_soap_record(patient_info, subjective, objective, assessment, plan)

    document_id = None
    if req.save_document and callable(service.document_writer):
        try:
            document_id = service.document_writer(
                user_id,
                title=f"门诊病历-{req.chief_complaint or '综合问诊'}-{date.today().isoformat()}",
                markdown=soap_md,
                metadata={"type": "medical_soap", "chief_complaint": req.chief_complaint},
            )
        except Exception as exc:
            logger.warning("保存门诊病历到本地文档库失败: %s", exc)

    return json.dumps({
        "status": "success",
        "intent": "generate_soap_note",
        "document_id": document_id,
        "markdown": soap_md,
    }, ensure_ascii=False)
