<template>
  <div class="medical-overlay" :class="{ 'fullscreen-mode': isFullScreen }" role="dialog" aria-modal="true" aria-labelledby="medical-title" @click.self="$emit('close')">
    <section class="medical-shell">
      <!-- 顶部 Header -->
      <header class="medical-header">
        <div class="medical-brand">
          <span class="medical-logo" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M12 4v16m-8-8h16" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>
          </span>
          <div>
            <div class="medical-kicker">Clinical Decision Support System (CDSS)</div>
            <h1 id="medical-title">智慧云诊室 · 临床辅助决策工作台</h1>
          </div>
        </div>

        <!-- 核心视图切换器 (Segmented Control) -->
        <div class="view-mode-switcher" role="group" aria-label="工作台视图切换">
          <button
            type="button"
            class="switch-btn"
            :class="{ active: viewMode === 'workstation' }"
            @click="viewMode = 'workstation'"
            title="仿 DISC-MedLLM & OpenMedLab 三栏医生决策工作站"
          >
            <span class="switch-icon">🏥</span>
            <span class="switch-text">三栏临床工作台</span>
          </button>
          <button
            type="button"
            class="switch-btn"
            :class="{ active: viewMode === 'consultation' }"
            @click="viewMode = 'consultation'"
            title="仿 HuatuoGPT & AI-Doctor 多专家协同会诊流"
          >
            <span class="switch-icon">💬</span>
            <span class="switch-text">多专家会诊流</span>
          </button>
          <button
            type="button"
            class="switch-btn"
            :class="{ active: viewMode === 'toolbox' }"
            @click="viewMode = 'toolbox'"
            title="临床指标试算器与用药安全独立工具箱"
          >
            <span class="switch-icon">🧰</span>
            <span class="switch-text">独立工具箱</span>
          </button>
        </div>

        <div class="medical-header-actions">
          <div class="status-indicator" :class="{ emergency: emergencyAlert?.triggered }">
            <span class="pulse-dot"></span>
            <span>{{ emergencyAlert?.triggered ? '🚨 S0/S1 急危重症触发' : '🟢 S0/S1 拦截门禁在线' }}</span>
          </div>
          <button class="icon-button" type="button" :title="isFullScreen ? '退出全屏' : '全屏显示'" @click="isFullScreen = !isFullScreen">
            <svg viewBox="0 0 24 24" v-if="!isFullScreen"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>
            <svg viewBox="0 0 24 24" v-else><path d="M4 14h6v6M20 10h-6V4M10 14l-7 7M14 10l7-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>
          </button>
          <button class="icon-button close-btn" type="button" aria-label="关闭智慧云诊室" @click="$emit('close')">
            <svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          </button>
        </div>
      </header>

      <!-- 全局 S0 急危重症脉冲呼吸条 (当触发红线时滑出) -->
      <transition name="slide-down">
        <div v-if="emergencyAlert?.triggered" class="emergency-pulse-banner">
          <div class="emergency-pulse-left">
            <span class="alarm-badge">S0 红色拦截</span>
            <div class="alert-info">
              <strong>【急危重症预警】{{ emergencyAlert.alert?.condition_name }}</strong>
              <p>{{ emergencyAlert.alert?.action_guide }}</p>
            </div>
          </div>
          <div class="emergency-pulse-actions">
            <button class="emergency-action-btn primary" type="button" @click="handleCall120">
              📞 模拟呼叫 120 急救中心
            </button>
            <button class="emergency-action-btn secondary" type="button" @click="handleInsertEmergencyToSOAP">
              📋 一键沉淀急救处置至病历
            </button>
          </div>
        </div>
      </transition>

      <!-- ============================================================== -->
      <!-- 视图 1: 旗舰级三栏临床决策工作台 (DISC-MedLLM & OpenMedLab CDSS) -->
      <!-- ============================================================== -->
      <div v-if="viewMode === 'workstation'" class="workstation-grid">
        <!-- 左栏：患者全息画像与典型案例 -->
        <aside class="col-patient">
          <div class="panel-card dossier-card">
            <div class="panel-header">
              <span class="panel-title">👤 患者健康画像</span>
              <span class="patient-id-badge">ID: P-{{ activePatient.id }}</span>
            </div>

            <div class="patient-profile">
              <div class="profile-main">
                <div class="patient-avatar">{{ activePatient.gender === '男' ? '👨' : '👩' }}</div>
                <div class="patient-meta">
                  <div class="name-line">
                    <span class="p-name">{{ activePatient.name }}</span>
                    <span class="p-tag">{{ activePatient.gender }} · {{ activePatient.age }}岁</span>
                  </div>
                  <div class="p-dept">就诊科室：{{ activePatient.dept }}</div>
                </div>
              </div>

              <!-- 生命体征看板 -->
              <div class="vitals-row">
                <div class="vital-item">
                  <span class="vital-lbl">血压</span>
                  <span class="vital-val" :class="{ warn: activePatient.vitals.bp_warn }">{{ activePatient.vitals.bp }}</span>
                </div>
                <div class="vital-item">
                  <span class="vital-lbl">心率</span>
                  <span class="vital-val">{{ activePatient.vitals.hr }} <small>bpm</small></span>
                </div>
                <div class="vital-item">
                  <span class="vital-lbl">BMI</span>
                  <span class="vital-val" :class="{ warn: activePatient.vitals.bmi > 24 }">{{ activePatient.vitals.bmi }}</span>
                </div>
              </div>

              <!-- 已知过敏与警示 -->
              <div class="allergy-strip" v-if="activePatient.allergies?.length">
                <span class="allergy-tag" v-for="alg in activePatient.allergies" :key="alg">
                  ⚠️ 过敏：{{ alg }}
                </span>
              </div>
            </div>

            <!-- 问诊槽位雷达进度条 (HuatuoGPT 算法对齐) -->
            <div class="slot-tracker-card">
              <div class="slot-tracker-header">
                <span>🎯 临床要素采集进度</span>
                <span class="slot-score">{{ completedSlotCount }}/6 ({{ Math.round(completedSlotCount/6*100) }}%)</span>
              </div>
              <div class="progress-bar">
                <div class="progress-fill" :style="{ width: (completedSlotCount/6*100) + '%' }"></div>
              </div>
              <div class="slot-chip-grid">
                <div
                  v-for="(slot, idx) in slotsList"
                  :key="idx"
                  class="slot-chip"
                  :class="{ filled: slot.filled }"
                  :title="slot.filled ? '已采纳: ' + slot.value : '缺失要素: 点击追问'"
                  @click="!slot.filled && askSlotFollowup(slot.key)"
                >
                  <span class="chip-status">{{ slot.filled ? '✓' : '?' }}</span>
                  <span class="chip-label">{{ slot.label }}</span>
                </div>
              </div>
            </div>

            <!-- 典型基准病例库 (开源 Benchmark 一键载入) -->
            <div class="preset-cases-box">
              <div class="box-title">📋 载入临床开源评测病例</div>
              <div class="preset-case-list">
                <button
                  v-for="c in presetCases"
                  :key="c.id"
                  class="preset-case-btn"
                  :class="{ active: activeCaseId === c.id }"
                  type="button"
                  @click="loadPresetCase(c)"
                >
                  <span class="case-level" :class="c.levelClass">{{ c.level }}</span>
                  <span class="case-name">{{ c.title }}</span>
                </button>
              </div>
            </div>
          </div>
        </aside>

        <!-- 中栏：MDT 智能协同会诊区 -->
        <main class="col-consultation">
          <div class="panel-card chat-card">
            <div class="panel-header">
              <div class="header-left">
                <span class="panel-title">💬 MDT 多学科协同会诊流</span>
                <span class="badge-tag">HuatuoGPT & AI-Doctor 双核</span>
              </div>
              <div class="header-actions">
                <button class="sm-btn" type="button" @click="clearConsultation">清空讨论</button>
              </div>
            </div>

            <!-- 会诊讨论消息流 -->
            <div class="consultation-stream" ref="consultationStreamRef">
              <div v-for="(msg, idx) in consultationMessages" :key="idx" class="mdt-message-bubble" :class="msg.role">
                <div class="avatar-cell">
                  <span class="agent-avatar">{{ msg.avatar }}</span>
                </div>
                <div class="content-cell">
                  <div class="sender-head">
                    <span class="sender-name">{{ msg.senderName }}</span>
                    <span class="sender-role-tag" :class="msg.badgeClass">{{ msg.roleTag }}</span>
                    <span class="msg-time">{{ msg.time }}</span>
                  </div>

                  <div class="message-text" v-html="formatMessage(msg.text)"></div>

                  <!-- 结构化思维链折叠面板 -->
                  <div v-if="msg.cotTrace" class="cot-trace-box">
                    <details>
                      <summary>🔍 查看临床思维链 (CoT) 与证据链</summary>
                      <pre class="cot-content">{{ msg.cotTrace }}</pre>
                    </details>
                  </div>

                  <!-- 结构化用药风险预警 -->
                  <div v-if="msg.safetyAlerts?.length" class="inline-safety-alerts">
                    <div v-for="(sa, sIdx) in msg.safetyAlerts" :key="sIdx" class="safety-pill" :class="sa.level">
                      <span>{{ sa.level === 'S0' ? '🚨' : '⚠️' }} {{ sa.text }}</span>
                    </div>
                  </div>

                  <!-- 医生人机核准操作条 -->
                  <div class="doctor-approval-bar">
                    <button class="approve-btn" type="button" :class="{ approved: msg.approved }" @click="msg.approved = !msg.approved">
                      {{ msg.approved ? '✓ 医生已核准采纳' : '○ 点击采纳该诊疗建议' }}
                    </button>
                    <button v-if="msg.canInsertSOAP" class="sync-soap-btn" type="button" @click="insertMsgToSOAP(msg)">
                      📑 沉淀到 SOAP
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <!-- 快捷下医嘱指令栏 -->
            <div class="quick-order-bar">
              <span class="quick-order-label">⚡ 快速临床指令:</span>
              <button class="order-chip" type="button" @click="handleQuickOrder('心电图与心肌酶谱(Trop-I)急查')">查肌钙蛋白</button>
              <button class="order-chip" type="button" @click="handleQuickOrder('根据年龄与肌酐计算 eGFR 肾功能')">核算 eGFR</button>
              <button class="order-chip" type="button" @click="handleQuickOrder('审查当前处方是否存在药物配伍禁忌(DDI)')">排查配伍禁忌</button>
              <button class="order-chip" type="button" @click="handleQuickOrder('根据主诉与检验结果，生成规范门诊 SOAP 病历')">规范生成病历</button>
            </div>

            <!-- 底部输入框 -->
            <div class="chat-input-row">
              <textarea
                v-model="inputQuery"
                class="chat-textarea"
                rows="2"
                placeholder="输入患者主诉、检验检查指标或下达会诊指令（如：'患者胸痛30分钟服用硝酸甘油，排查用药风险'）..."
                @keydown.enter.prevent="sendConsultation"
              ></textarea>
              <button class="btn primary send-btn" type="button" :disabled="loadingAgent || !inputQuery.trim()" @click="sendConsultation">
                {{ loadingAgent ? '会诊中...' : '发送指令' }}
              </button>
            </div>
          </div>
        </main>

        <!-- 右栏：CDSS 辅助决策支持 & SOAP 实时病历 -->
        <aside class="col-cdss">
          <div class="panel-card cdss-card">
            <!-- 切换选项卡 -->
            <div class="cdss-tabs">
              <button type="button" :class="{ active: cdssTab === 'soap' }" @click="cdssTab = 'soap'">
                📄 SOAP 电子病历
              </button>
              <button type="button" :class="{ active: cdssTab === 'calc' }" @click="cdssTab = 'calc'">
                📊 临床计算器
              </button>
              <button type="button" :class="{ active: cdssTab === 'ddi' }" @click="cdssTab = 'ddi'">
                💊 处方安全审计
              </button>
            </div>

            <div class="cdss-body">
              <!-- CDSS Tab 1: 实时 SOAP 电子病历 -->
              <div v-if="cdssTab === 'soap'" class="cdss-pane soap-pane">
                <div class="soap-doc-header">
                  <div class="soap-title">门诊就诊记录 (SOAP)</div>
                  <div class="soap-actions">
                    <button class="icon-text-btn" type="button" @click="copySOAPMarkdown">📋 复制</button>
                    <button class="icon-text-btn primary" type="button" :disabled="loadingSoap" @click="saveSOAPToRAG">
                      {{ savingDoc ? '正在归档...' : '📥 存入知识库' }}
                    </button>
                  </div>
                </div>

                <div class="soap-sections">
                  <!-- S: Subjective -->
                  <div class="soap-sec s-sec">
                    <div class="sec-tag">S · 主观资料 (Subjective)</div>
                    <div class="sec-content">
                      <p><strong>主诉：</strong>{{ soapForm.chiefComplaint || '暂无，等待录入...' }}</p>
                      <p><strong>现病史：</strong>{{ soapForm.hpi || '暂无详细描述' }}</p>
                    </div>
                  </div>

                  <!-- O: Objective -->
                  <div class="soap-sec o-sec">
                    <div class="sec-tag">O · 客观体征与检验 (Objective)</div>
                    <div class="sec-content">
                      <p><strong>生命体征：</strong>BP {{ activePatient.vitals.bp }}, HR {{ activePatient.vitals.hr }} bpm, BMI {{ activePatient.vitals.bmi }}</p>
                      <p><strong>辅助检验：</strong>{{ soapForm.objective || '待完善心电图/血液生化指标' }}</p>
                    </div>
                  </div>

                  <!-- A: Assessment -->
                  <div class="soap-sec a-sec">
                    <div class="sec-tag">A · 临床评估与诊断 (Assessment)</div>
                    <div class="sec-content">
                      <p><strong>拟诊：</strong>{{ currentAssessment || '根据主诉与检查综合研判中' }}</p>
                      <p v-if="emergencyAlert?.triggered" class="red-alert-text">
                        🚨 存在急危重症红线：{{ emergencyAlert.alert?.condition_name }}
                      </p>
                    </div>
                  </div>

                  <!-- P: Plan -->
                  <div class="soap-sec p-sec">
                    <div class="sec-tag">P · 处置与处方计划 (Plan)</div>
                    <div class="sec-content">
                      <p><strong>处置建议：</strong>{{ currentPlan || '1. 完善常规检查；2. 监测生命体征。' }}</p>
                    </div>
                  </div>
                </div>
              </div>

              <!-- CDSS Tab 2: 临床计算器微组件 -->
              <div v-if="cdssTab === 'calc'" class="cdss-pane calc-pane">
                <!-- GFR 计算 -->
                <div class="micro-calc-box">
                  <div class="micro-head">
                    <span>Cockcroft-Gault eGFR 肾功能</span>
                    <button class="micro-run-btn" type="button" :disabled="loadingCalc" @click="runGFR">计算</button>
                  </div>
                  <div class="micro-inputs">
                    <input v-model.number="gfrForm.age" type="number" placeholder="年龄" />
                    <input v-model.number="gfrForm.weight" type="number" placeholder="体重kg" />
                    <input v-model.number="gfrForm.scr" type="number" placeholder="肌酐μmol/L" />
                  </div>
                  <div v-if="gfrResult" class="micro-result">
                    <span class="res-num">{{ gfrResult.value }} ml/min</span>
                    <span class="res-badge">{{ gfrResult.stage }}</span>
                    <button class="insert-btn" type="button" @click="insertGFRToSOAP">填入病历</button>
                  </div>
                </div>

                <!-- CHA2DS2-VASc 卒中评分 -->
                <div class="micro-calc-box">
                  <div class="micro-head">
                    <span>CHA2DS2-VASc 房颤卒中评分</span>
                    <button class="micro-run-btn" type="button" :disabled="loadingCalc" @click="runCHADS">评估</button>
                  </div>
                  <div class="chads-checks">
                    <label><input type="checkbox" v-model="chadsForm.chf" /> 心衰</label>
                    <label><input type="checkbox" v-model="chadsForm.hypertension" /> 高血压</label>
                    <label><input type="checkbox" v-model="chadsForm.diabetes" /> 糖尿病</label>
                    <label><input type="checkbox" v-model="chadsForm.stroke" /> 卒中病史(+2)</label>
                  </div>
                  <div v-if="chadsResult" class="micro-result">
                    <span class="res-num">{{ chadsResult.value }} 分</span>
                    <span class="res-badge" :class="chadsResult.risk_category === '高危' ? 'warn' : ''">{{ chadsResult.risk_category }}</span>
                    <button class="insert-btn" type="button" @click="insertCHADSToSOAP">填入病历</button>
                  </div>
                </div>

                <!-- BMI 计算 -->
                <div class="micro-calc-box">
                  <div class="micro-head">
                    <span>BMI 成人营养标准</span>
                    <button class="micro-run-btn" type="button" :disabled="loadingCalc" @click="runBMI">计算</button>
                  </div>
                  <div class="micro-inputs">
                    <input v-model.number="bmiForm.height" type="number" placeholder="身高cm" />
                    <input v-model.number="bmiForm.weight" type="number" placeholder="体重kg" />
                  </div>
                  <div v-if="bmiResult" class="micro-result">
                    <span class="res-num">{{ bmiResult.value }} kg/m²</span>
                    <span class="res-badge">{{ bmiResult.category }}</span>
                    <button class="insert-btn" type="button" @click="insertBMIToSOAP">填入病历</button>
                  </div>
                </div>
              </div>

              <!-- CDSS Tab 3: 实时处方与配伍禁忌 -->
              <div v-if="cdssTab === 'ddi'" class="cdss-pane ddi-pane">
                <div class="ddi-panel-box">
                  <div class="box-title">💊 实时用药安全矩阵排查</div>
                  <div class="ddi-input-area">
                    <label class="sm-label">拟开具/已服用药物 (顿号分隔)</label>
                    <input v-model="safetyForm.drugs" class="full-input" placeholder="例如: 硝酸甘油、西地那非、阿司匹林" />
                    <label class="sm-label">患者已知过敏原</label>
                    <input v-model="safetyForm.allergies" class="full-input" placeholder="例如: 青霉素、头孢" />
                    <button class="btn primary full-btn" type="button" :disabled="loadingSafety" @click="runSafetyCheck">
                      {{ loadingSafety ? '排查中...' : '🔍 立即进行处方安全审计' }}
                    </button>
                  </div>

                  <div v-if="safetyResult" class="ddi-result-area">
                    <div class="audit-status" :class="safetyResult.safe ? 'safe' : 'danger'">
                      {{ safetyResult.safe ? '✅ 未发现明显配伍禁忌或过敏冲突' : '🚨 发现用药安全风险，需人工干预！' }}
                    </div>
                    <div v-for="(conf, cIdx) in safetyResult.conflicts" :key="cIdx" class="ddi-conflict-item">
                      <div class="conf-head">
                        <span class="pair">{{ conf.pair?.join(' + ') }}</span>
                        <span class="level" :class="conf.severity">{{ conf.severity }}</span>
                      </div>
                      <div class="conf-desc">{{ conf.description }}</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </aside>
      </div>

      <!-- ============================================================== -->
      <!-- 视图 2: HuatuoGPT & AI-Doctor 多专家协同会诊流 (全屏对话视角) -->
      <!-- ============================================================== -->
      <div v-else-if="viewMode === 'consultation'" class="consultation-full-view">
        <!-- 顶部活跃槽位进度条 -->
        <div class="consult-top-bar">
          <div class="patient-pill-summary">
            <span class="p-avatar">👨‍⚕️</span>
            <strong>当前就诊人：{{ activePatient.name }}（{{ activePatient.gender }}，{{ activePatient.age }}岁）</strong>
            <span class="p-divider">|</span>
            <span>过敏史：{{ activePatient.allergies?.join('、') || '无已知药物过敏' }}</span>
          </div>

          <!-- 槽位进度 -->
          <div class="slots-inline-tracker">
            <span class="tracker-title">问诊槽位：</span>
            <div v-for="(slot, idx) in slotsList" :key="idx" class="slot-pill" :class="{ filled: slot.filled }">
              <span class="dot"></span>
              <span>{{ slot.label }}</span>
            </div>
          </div>
        </div>

        <!-- 对话流中心卡片 -->
        <div class="consultation-flow-container">
          <div class="flow-message-list">
            <div v-for="(msg, idx) in consultationMessages" :key="idx" class="flow-msg-row" :class="msg.role">
              <div class="msg-avatar-badge">{{ msg.avatar }}</div>
              <div class="msg-body-wrapper">
                <div class="msg-meta-line">
                  <span class="name">{{ msg.senderName }}</span>
                  <span class="role-badge" :class="msg.badgeClass">{{ msg.roleTag }}</span>
                  <span class="time">{{ msg.time }}</span>
                </div>
                <div class="msg-bubble" v-html="formatMessage(msg.text)"></div>

                <!-- 临床推理思维链折叠 -->
                <div v-if="msg.cotTrace" class="cot-flow-box">
                  <details>
                    <summary>🧠 临床思维链推演 (CoT)</summary>
                    <div class="cot-text">{{ msg.cotTrace }}</div>
                  </details>
                </div>

                <!-- 医生审核确认按钮 -->
                <div class="doctor-action-btns">
                  <button class="doc-btn" :class="{ done: msg.approved }" @click="msg.approved = !msg.approved">
                    {{ msg.approved ? '✓ 医生已审核采纳' : '○ 医生审核通过' }}
                  </button>
                  <button class="doc-btn soap" @click="insertMsgToSOAP(msg)">
                    📥 记录至病历
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 底部快捷主诉胶囊 & 输入 -->
        <div class="consultation-bottom-dock">
          <div class="quick-pill-row">
            <span class="dock-label">💡 常见主诉快捷输入:</span>
            <button class="pill-btn" type="button" @click="handleQuickTriage('突发胸骨后压榨性剧痛30分钟，向左肩放射伴大汗淋漓')">
              💔 心梗 S0 (胸痛放射)
            </button>
            <button class="pill-btn" type="button" @click="handleQuickTriage('老人早晨突发口角歪斜，右侧手脚无力无法抬起，吐字不清')">
              🧠 脑卒中 FAST (口角歪斜偏瘫)
            </button>
            <button class="pill-btn" type="button" @click="handleQuickTriage('冠心病患者服用硝酸甘油，昨天又服用了西地那非，排查用药风险')">
              💊 DDI 禁忌 (硝酸甘油+西地那非)
            </button>
            <button class="pill-btn" type="button" @click="handleQuickTriage('咽喉肿痛发热，有青霉素过敏史，请问能否服用阿莫西林')">
              🚫 青霉素交叉过敏
            </button>
          </div>

          <div class="flow-input-bar">
            <textarea
              v-model="inputQuery"
              class="flow-input"
              rows="2"
              placeholder="输入患者的主诉、症状描述或临床医嘱进行多专家协同问诊（Enter 发送）..."
              @keydown.enter.prevent="sendConsultation"
            ></textarea>
            <button class="btn primary flow-send-btn" type="button" :disabled="loadingAgent || !inputQuery.trim()" @click="sendConsultation">
              {{ loadingAgent ? '会诊中...' : '发送问诊' }}
            </button>
          </div>
        </div>
      </div>

      <!-- ============================================================== -->
      <!-- 视图 3: 独立临床计算工具箱 (Calculators & DDI & SOAP)            -->
      <!-- ============================================================== -->
      <div v-else-if="viewMode === 'toolbox'" class="toolbox-view">
        <nav class="sub-tab-nav">
          <button type="button" :class="{ active: toolboxSubTab === 'calc' }" @click="toolboxSubTab = 'calc'">
            📊 临床医学计算器 (BMI / GFR / 房颤卒中)
          </button>
          <button type="button" :class="{ active: toolboxSubTab === 'safety' }" @click="toolboxSubTab = 'safety'">
            💊 处方安全审查 (DDI & 过敏排查)
          </button>
          <button type="button" :class="{ active: toolboxSubTab === 'soap' }" @click="toolboxSubTab = 'soap'">
            📝 门诊 SOAP 电子病历独立生成
          </button>
        </nav>

        <div class="toolbox-body">
          <!-- 工具箱 Tab 1: 临床计算器 -->
          <div v-if="toolboxSubTab === 'calc'" class="calc-grid">
            <div class="calc-card">
              <h3>体质指数 (BMI)</h3>
              <p class="calc-desc">依据 WS/T 428-2013 成人体重判定标准计算</p>
              <div class="form-row">
                <label>身高 (cm) <input v-model.number="bmiForm.height" type="number" placeholder="175" /></label>
                <label>体重 (kg) <input v-model.number="bmiForm.weight" type="number" placeholder="70" /></label>
              </div>
              <button class="btn primary" type="button" :disabled="loadingCalc" @click="runBMI">
                {{ loadingCalc ? '计算中...' : '计算 BMI' }}
              </button>
              <div v-if="bmiResult" class="result-box">
                <div class="result-header">
                  <span class="val">{{ bmiResult.value }} <small>kg/m²</small></span>
                  <span class="badge" :class="bmiBadgeClass">{{ bmiResult.category }}</span>
                </div>
                <div class="advice">{{ bmiResult.recommendation }}</div>
              </div>
            </div>

            <div class="calc-card">
              <h3>内生肌酐清除率 (eGFR)</h3>
              <p class="calc-desc">Cockcroft-Gault 公式评估肾功能及用药指导</p>
              <div class="form-row">
                <label>年龄 (岁) <input v-model.number="gfrForm.age" type="number" placeholder="60" /></label>
                <label>体重 (kg) <input v-model.number="gfrForm.weight" type="number" placeholder="65" /></label>
              </div>
              <div class="form-row">
                <label>血肌酐 (μmol/L) <input v-model.number="gfrForm.scr" type="number" placeholder="120" /></label>
                <label>性别
                  <select v-model="gfrForm.sex">
                    <option value="male">男性</option>
                    <option value="female">女性</option>
                  </select>
                </label>
              </div>
              <button class="btn primary" type="button" :disabled="loadingCalc" @click="runGFR">
                {{ loadingCalc ? '计算中...' : '计算 eGFR' }}
              </button>
              <div v-if="gfrResult" class="result-box">
                <div class="result-header">
                  <span class="val">{{ gfrResult.value }} <small>ml/min</small></span>
                  <span class="stage-tag">{{ gfrResult.stage }}</span>
                </div>
                <div class="advice">{{ gfrResult.clinical_advice }}</div>
              </div>
            </div>

            <div class="calc-card">
              <h3>CHA2DS2-VASc 卒中风险评分</h3>
              <p class="calc-desc">2020 ESC 房颤抗凝决策指导</p>
              <div class="chads-grid">
                <label><input type="checkbox" v-model="chadsForm.chf" /> 充血性心衰 (+1)</label>
                <label><input type="checkbox" v-model="chadsForm.hypertension" /> 高血压 (+1)</label>
                <label><input type="checkbox" v-model="chadsForm.diabetes" /> 糖尿病 (+1)</label>
                <label><input type="checkbox" v-model="chadsForm.stroke" /> 既往卒中/TIA (+2)</label>
                <label><input type="checkbox" v-model="chadsForm.vascular" /> 血管疾病史 (+1)</label>
              </div>
              <button class="btn primary" type="button" :disabled="loadingCalc" @click="runCHADS">
                {{ loadingCalc ? '评估中...' : '评估卒中评分' }}
              </button>
              <div v-if="chadsResult" class="result-box">
                <div class="result-header">
                  <span class="val">{{ chadsResult.value }} <small>分</small></span>
                  <span class="badge" :class="chadsResult.risk_category === '高危' ? 'badge-danger' : 'badge-warning'">
                    {{ chadsResult.risk_category }}
                  </span>
                </div>
                <div class="advice">{{ chadsResult.recommendation }}</div>
              </div>
            </div>
          </div>

          <!-- 工具箱 Tab 2: 处方安全 -->
          <div v-if="toolboxSubTab === 'safety'" class="safety-pane">
            <div class="calc-card safety-card-full">
              <h3>处方安全与配伍禁忌排查</h3>
              <div class="form-group">
                <label>药品名称 (顿号分隔)</label>
                <input v-model="safetyForm.drugs" class="full-input" placeholder="华法林、阿司匹林、硝酸甘油" />
              </div>
              <div class="form-group">
                <label>已知过敏原</label>
                <input v-model="safetyForm.allergies" class="full-input" placeholder="青霉素" />
              </div>
              <button class="btn primary" type="button" :disabled="loadingSafety" @click="runSafetyCheck">
                {{ loadingSafety ? '排查中...' : '执行处方安全审计' }}
              </button>
              <div v-if="safetyResult" class="result-box ddi-box">
                <div class="status-banner" :class="safetyResult.safe ? 'banner-safe' : 'banner-s0'">
                  {{ safetyResult.safe ? '✅ 未发现明显禁忌' : '🚨 发现用药安全风险' }}
                </div>
                <div v-for="(conf, idx) in safetyResult.conflicts" :key="idx" class="alert-item">
                  <strong>{{ conf.pair?.join(' + ') }}</strong>: {{ conf.description }}
                </div>
              </div>
            </div>
          </div>

          <!-- 工具箱 Tab 3: SOAP 病历 -->
          <div v-if="toolboxSubTab === 'soap'" class="soap-layout">
            <div class="soap-input-card">
              <h3>门诊预问诊与病历要素</h3>
              <div class="form-row">
                <label>患者姓名 <input v-model="soapForm.patientName" type="text" /></label>
                <label>年龄 <input v-model="soapForm.patientAge" type="text" /></label>
                <label>性别
                  <select v-model="soapForm.patientGender">
                    <option value="男">男</option>
                    <option value="女">女</option>
                  </select>
                </label>
              </div>
              <div class="form-group">
                <label>主诉</label>
                <input v-model="soapForm.chiefComplaint" type="text" />
              </div>
              <div class="form-group">
                <label>现病史与既往史</label>
                <textarea v-model="soapForm.hpi" rows="3"></textarea>
              </div>
              <div class="form-group">
                <label>体征与检查</label>
                <textarea v-model="soapForm.objective" rows="2"></textarea>
              </div>
              <button class="btn primary" type="button" :disabled="loadingSoap" @click="runGenerateSOAP">
                {{ loadingSoap ? '生成中...' : '📑 生成规范门诊 SOAP 病历' }}
              </button>
            </div>
            <div class="soap-output-card" v-if="soapResultMarkdown">
              <div class="output-actions">
                <span>门诊记录预览</span>
                <button type="button" class="text-btn" @click="copySOAPMarkdown">复制 Markdown</button>
              </div>
              <pre class="markdown-preview">{{ soapResultMarkdown }}</pre>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'
import { fetchJSON } from '../api/client'

defineEmits(['close'])

// 全局与视图控制
const isFullScreen = ref(false)
const viewMode = ref('workstation') // 'workstation' | 'consultation' | 'toolbox'
const toolboxSubTab = ref('calc')
const cdssTab = ref('soap')

// 全局急诊与警报状态
const emergencyAlert = ref(null)

// 患者与槽位状态
const activeCaseId = ref('c1')
const activePatient = ref({
  id: '10842',
  name: '华建国',
  age: 62,
  gender: '男',
  dept: '心血管内科门诊',
  vitals: { bp: '145/92', hr: 88, bmi: 27.2, bp_warn: true },
  allergies: ['青霉素 (皮试阳性)'],
})

const slotsList = ref([
  { key: 'location', label: '主诉部位', filled: true, value: '胸骨后心前区' },
  { key: 'duration', label: '发病时长', filled: true, value: '30分钟' },
  { key: 'quality', label: '疼痛性质', filled: true, value: '压榨感/绞痛' },
  { key: 'radiation', label: '放射区域', filled: true, value: '向左肩背放射' },
  { key: 'symptoms', label: '伴随症状', filled: true, value: '大汗淋漓/恶心' },
  { key: 'history', label: '既往与用药', filled: false, value: '' },
])

const completedSlotCount = computed(() => slotsList.value.filter(s => s.filled).length)

// 4 大典型开源基准病例
const presetCases = ref([
  {
    id: 'c1',
    level: 'S0 急危',
    levelClass: 'danger',
    title: '冠心病疑似ACS伴西地那非使用',
    patient: {
      id: '10842',
      name: '华建国',
      age: 62,
      gender: '男',
      dept: '心血管内科门诊',
      vitals: { bp: '145/92', hr: 88, bmi: 27.2, bp_warn: true },
      allergies: ['青霉素 (皮试阳性)'],
    },
    chiefComplaint: '突发胸骨后压榨性剧痛30分钟，向左肩放射伴大汗淋漓',
    hpi: '患者30分钟前剧烈活动后突发胸骨后压榨性疼痛，痛感剧烈，放射至左肩背部，伴大汗。自服硝酸甘油1片未见明显缓解，前日因勃起功能障碍曾服用西地那非。',
    objective: '血压 145/92 mmHg, 心率 88次/分, 双肺呼吸音清, 心界无扩大, 心音低钝。心电图急查示：V1-V4导联ST段抬高0.2mV。',
    assessment: '1. 冠状动脉粥样硬化性心脏病，急性前壁心肌梗死（S0级急症）；2. 西地那非与硝酸甘油联用风险。',
    plan: '1. 立即启动导管室急诊PCI流程；2. 严密监测血压，绝对禁忌继续使用硝酸酯类药物；3. 绝对平卧，持续吸氧。',
    drugs: '硝酸甘油、西地那非',
    allergies: '青霉素',
  },
  {
    id: 'c2',
    level: 'S1 风险',
    levelClass: 'warning',
    title: '老年房颤伴 CKD3 肾功能减退',
    patient: {
      id: '10915',
      name: '李淑芬',
      age: 71,
      gender: '女',
      dept: '心内科/肾内科',
      vitals: { bp: '138/82', hr: 96, bmi: 22.8, bp_warn: false },
      allergies: [],
    },
    chiefComplaint: '阵发性心慌气促2周，加重2天',
    hpi: '患者2周来反复出现心慌心悸，活动后气促明显。既往有高血压病史10年，慢性肾脏病史2年。长期服用降压药。',
    objective: '心律绝对不齐，第一心音强弱不等，脉搏短绌。血肌酐 155 μmol/L, 尿素氮 9.8 mmol/L。',
    assessment: '1. 非瓣膜性心房颤动（高卒中风险，CHA2DS2-VASc 4分）；2. 慢性肾脏病（CKD 3b期）；3. 原发性高血压3级。',
    plan: '1. 评估抗凝方案（NOAC 需依据 eGFR 剂量折半调整）；2. 控制心室率；3. 肾功能保护治疗。',
    drugs: '华法林、阿司匹林',
    allergies: '',
  },
  {
    id: 'c3',
    level: '安全审查',
    levelClass: 'info',
    title: '急性支气管炎伴青霉素过敏',
    patient: {
      id: '10988',
      name: '王雪',
      age: 34,
      gender: '女',
      dept: '呼吸内科门诊',
      vitals: { bp: '118/76', hr: 76, bmi: 21.0, bp_warn: false },
      allergies: ['青霉素 (严重全身皮疹过敏)'],
    },
    chiefComplaint: '咳嗽咳黄痰4天伴发热',
    hpi: '4天前受凉后出现咳嗽、咯黏稠黄痰，伴发热（体温最高38.3℃）。自诉既往有明确青霉素严重过敏史。',
    objective: '双肺呼吸音粗，双下肺可闻及散在干啰音。血常规：WBC 11.2×10^9/L，中性粒细胞占比 78%。',
    assessment: '1. 急性支气管炎（细菌性可能大）；2. 明确青霉素过敏史。',
    plan: '1. 绝对禁用青霉素类及阿莫西林等；2. 建议换用阿奇霉素或左氧氟沙星；3. 祛痰对症支持。',
    drugs: '阿莫西林、布洛芬',
    allergies: '青霉素',
  },
  {
    id: 'c4',
    level: '慢病管理',
    levelClass: 'normal',
    title: '痛风性关节炎合并代谢综合征',
    patient: {
      id: '11024',
      name: '赵大明',
      age: 46,
      gender: '男',
      dept: '内分泌/风湿科',
      vitals: { bp: '142/90', hr: 80, bmi: 30.2, bp_warn: true },
      allergies: [],
    },
    chiefComplaint: '右侧足第一跖趾关节突发红肿热痛1天',
    hpi: '昨日进食海鲜饮啤酒后，夜间突发右足第一跖趾关节剧烈疼痛，局部红肿灼热，行走受限。体型肥胖，喜好高嘌呤饮食。',
    objective: '右第1跖趾关节局部明显红肿、皮温升高，触痛显著。血尿酸 540 μmol/L，空腹血糖 6.8 mmol/L。',
    assessment: '1. 急性痛风性关节炎；2. 高尿酸血症；3. 肥胖症（BMI 30.2）；4. 代谢综合征。',
    plan: '1. 急性期秋水仙碱或NSAIDs抗炎止痛；2. 缓解期规律降尿酸治疗；3. 严格低嘌呤低脂饮食管理。',
    drugs: '秋水仙碱、塞来昔布',
    allergies: '',
  },
])

// 会诊消息流
const consultationMessages = ref([
  {
    role: 'agent',
    avatar: '👨‍⚕️',
    senderName: '分诊主治医生',
    roleTag: 'MedicalTriageAgent',
    badgeClass: 'triage-badge',
    time: '09:30',
    text: '您好，我是智能临床分诊主治医生。已接入患者【华建国】健康档案。患者主诉胸骨后压榨性疼痛，已触发疑似急性心肌梗死筛查，请密切配合采集病史。',
    cotTrace: '【思维链剖析】\n1. 输入主诉："突发胸骨后压榨性剧痛30分钟，向左肩放射伴大汗淋漓"\n2. 匹配急症红线：急性冠脉综合征 (ACS)，严重级别 S0。\n3. 缺失要素识别：发作时含服硝酸甘油效果如何？既往有无类似发作？',
    approved: true,
    canInsertSOAP: true,
  },
  {
    role: 'agent',
    avatar: '💊',
    senderName: '临床用药审查药师',
    roleTag: 'DrugSafetyReviewAgent',
    badgeClass: 'safety-badge',
    time: '09:31',
    text: '🚨【S0 绝对禁忌拦截】检测到患者自述近期服用过【西地那非】，并含服了【硝酸甘油】！\n\n硝酸甘油与西地那非联用会引起极度强烈的协同血管舒张，可导致致死性顽固低血压甚至心源性休克！请主治医生立即禁止再次给予硝酸酯类药物！',
    cotTrace: '【DDI 排查引擎】\n- 药物对：硝酸甘油 (Nitroglycerin) + 西地那非 (Sildenafil)\n- 相互作用机制：PDE5 抑制剂阻断 cGMP 降解，硝酸酯类促进 NO 合成，二者协同致 cGMP 极度蓄积。\n- 风险分级：S0 绝对禁忌 (Contraindicated)',
    safetyAlerts: [{ level: 'S0', text: '硝酸甘油 + 西地那非：致死性顽固低血压禁忌' }],
    approved: true,
    canInsertSOAP: true,
  },
  {
    role: 'agent',
    avatar: '🩺',
    senderName: '心内科专科主治',
    roleTag: 'CardiologySpecialist',
    badgeClass: 'specialist-badge',
    time: '09:32',
    text: '患者心电图提示 V1-V4 导联 ST 段抬高，结合压榨性胸痛，高度疑似急性前壁 STEMI。立即通知心导管室就位，开通绿色通道！停用硝酸甘油，改用吸氧及监护。',
    cotTrace: '【鉴别诊断排序】\n1. 急性前壁 ST 段抬高型心肌梗死 (STEMI) - 概率 85%\n2. 急性主动脉夹层 - 概率 10% (需急查主动脉CTA排查)\n3. 急性肺动脉栓塞 - 概率 5%',
    approved: true,
    canInsertSOAP: true,
  },
])

const consultationStreamRef = ref(null)
const inputQuery = ref('')
const loadingAgent = ref(false)

// 当前评估与计划
const currentAssessment = ref('1. 冠心病，急性前壁心肌梗死疑似 (S0级急症)；2. 西地那非与硝酸甘油致命DDI风险。')
const currentPlan = ref('1. 立即呼叫120或启动心内科胸痛绿色通道；2. 停用所有硝酸酯类；3. 平卧吸氧及心电监护。')

// 临床计算器表单与结果
const loadingCalc = ref(false)
const bmiForm = ref({ height: 172, weight: 68 })
const bmiResult = ref(null)
const gfrForm = ref({ age: 62, weight: 65, scr: 110, sex: 'male' })
const gfrResult = ref(null)
const chadsForm = ref({ age: 72, sex: 'male', chf: true, hypertension: true, diabetes: false, stroke: false, vascular: false })
const chadsResult = ref(null)

const bmiBadgeClass = computed(() => {
  if (!bmiResult.value) return ''
  const cat = bmiResult.value.category
  if (cat.includes('正常')) return 'badge-success'
  if (cat.includes('超重')) return 'badge-warning'
  return 'badge-danger'
})

// 处方安全审计表单
const loadingSafety = ref(false)
const safetyForm = ref({
  drugs: '硝酸甘油、西地那非',
  allergies: '青霉素',
})
const safetyResult = ref(null)

// 门诊 SOAP 表单
const loadingSoap = ref(false)
const savingDoc = ref(false)
const soapResultMarkdown = ref('')
const soapForm = ref({
  patientName: '华建国',
  patientAge: '62',
  patientGender: '男',
  chiefComplaint: '突发胸骨后压榨性剧痛30分钟，向左肩放射伴大汗淋漓',
  hpi: '30分钟前剧烈运动后突发心前区剧烈压榨痛，向左肩放射，含服硝酸甘油未缓解，服过西地那非。',
  objective: 'BP 145/92 mmHg, 心电图示 V1-V4 导联 ST 段抬高。',
  saveDocument: false,
})

// ==========================================
// 逻辑方法
// ==========================================

function loadPresetCase(c) {
  activeCaseId.value = c.id
  activePatient.value = { ...c.patient }
  soapForm.value.patientName = c.patient.name
  soapForm.value.patientAge = String(c.patient.age)
  soapForm.value.patientGender = c.patient.gender
  soapForm.value.chiefComplaint = c.chiefComplaint
  soapForm.value.hpi = c.hpi
  soapForm.value.objective = c.objective
  currentAssessment.value = c.assessment
  currentPlan.value = c.plan
  safetyForm.value.drugs = c.drugs
  safetyForm.value.allergies = c.allergies

  // 触发紧急警报状态
  if (c.level.includes('S0')) {
    emergencyAlert.value = {
      triggered: true,
      alert: {
        condition_name: '急性心肌梗死 / 致命性低血压风险 (S0级)',
        action_guide: '立即停止活动平卧，严禁继续服用硝酸甘油，开通绿色通道或拨打 120！',
      },
    }
  } else {
    emergencyAlert.value = null
  }

  // 重置会诊流
  consultationMessages.value = [
    {
      role: 'agent',
      avatar: '👨‍⚕️',
      senderName: '分诊主治医生',
      roleTag: 'MedicalTriageAgent',
      badgeClass: 'triage-badge',
      time: '现在',
      text: `已载入病例【${c.title}】。患者主诉：${c.chiefComplaint}。已启动临床要素采集与多学科会诊。`,
      cotTrace: `【自动载入基准病例】\n- 预设诊断：${c.assessment}\n- 初步处置：${c.plan}`,
      approved: true,
      canInsertSOAP: true,
    },
  ]

  if (c.drugs) {
    runSafetyCheck()
  }
}

function handleCall120() {
  alert('🚨【紧急调度指令】已模拟联动 120 急救网络！患者【' + activePatient.value.name + '】急救绿色通道已建立，急救车调度中。')
}

function handleInsertEmergencyToSOAP() {
  if (!emergencyAlert.value) return
  const alertText = `【S0急救处置】${emergencyAlert.value.alert?.condition_name}。处理指引：${emergencyAlert.value.alert?.action_guide}。`
  currentPlan.value = alertText + '\n' + currentPlan.value
  alert('已将急救指令同步沉淀至 SOAP 处置计划 (Plan) 栏目！')
}

function askSlotFollowup(slotKey) {
  const map = {
    history: '请问患者既往有无高血压、糖尿病或类似发作病史？目前正在长期规律服用哪些药物？',
    radiation: '请问疼痛有无向左肩、背部、颈部或下颌部放射？',
    duration: '请问这种不适感持续多长时间了？是阵发性还是持续性加重？',
    symptoms: '发作时是否伴有胸闷气短、大汗淋漓、恶心呕吐或头晕黑曚？',
  }
  inputQuery.value = map[slotKey] || '请补充相关病史要素。'
}

function handleQuickOrder(order) {
  inputQuery.value = order
  sendConsultation()
}

function handleQuickTriage(text) {
  inputQuery.value = text
  sendConsultation()
}

async function sendConsultation() {
  const query = inputQuery.value.trim()
  if (!query) return

  // 1. 本地插入用户消息
  consultationMessages.value.push({
    role: 'user',
    avatar: '👤',
    senderName: '临床医生',
    roleTag: 'Physician',
    badgeClass: 'doctor-badge',
    time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    text: query,
    approved: true,
  })

  inputQuery.value = ''
  loadingAgent.value = true
  await scrollStream()

  // 2. 本地触发 S0/S1 快速筛查
  try {
    const emRes = await fetchJSON('/api/medical/emergency?query=' + encodeURIComponent(query), { method: 'POST' })
    if (emRes.triggered) {
      emergencyAlert.value = emRes
    }
  } catch (e) {
    console.warn('Emergency check err:', e)
  }

  // 3. 调用后端 Agent
  try {
    const resp = await fetchJSON('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: query, use_rag: false }),
    })

    const answer = resp.answer || '已记录临床指令。'
    consultationMessages.value.push({
      role: 'agent',
      avatar: '👨‍⚕️',
      senderName: '分诊会诊专家',
      roleTag: 'ClinicalCopilot',
      badgeClass: 'triage-badge',
      time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      text: answer,
      cotTrace: resp.thought || resp.extracted_info || '【自主推理】已结合当前多学科知识库与规则库进行确定性研判。',
      approved: false,
      canInsertSOAP: true,
    })

    // 如果回答包含处方或诊断，尝试提取更新 SOAP
    if (answer.includes('拟诊') || answer.includes('诊断')) {
      currentAssessment.value = answer.slice(0, 120) + '...'
    }
  } catch (err) {
    consultationMessages.value.push({
      role: 'agent',
      avatar: '⚠️',
      senderName: '系统提示',
      roleTag: 'System',
      badgeClass: 'danger-badge',
      time: '刚刚',
      text: '协同会诊响应异常: ' + err.message,
    })
  } finally {
    loadingAgent.value = false
    await scrollStream()
  }
}

async function scrollStream() {
  await nextTick()
  if (consultationStreamRef.value) {
    consultationStreamRef.value.scrollTop = consultationStreamRef.value.scrollHeight
  }
}

function clearConsultation() {
  consultationMessages.value = []
}

function insertMsgToSOAP(msg) {
  currentPlan.value += `\n- [会诊采纳] ${msg.senderName}建议：${msg.text.slice(0, 80)}`
  alert('已将该条会诊建议沉淀至 SOAP 处置计划中！')
}

function formatMessage(text) {
  if (!text) return ''
  return text.replace(/\n/g, '<br/>')
}

// 计算器操作
async function runBMI() {
  loadingCalc.value = true
  try {
    const res = await fetchJSON('/api/medical/score', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ score_type: 'bmi', params: { height_cm: bmiForm.value.height, weight_kg: bmiForm.value.weight } }),
    })
    bmiResult.value = res
  } catch (err) {
    alert('计算失败: ' + err.message)
  } finally {
    loadingCalc.value = false
  }
}

async function runGFR() {
  loadingCalc.value = true
  try {
    const res = await fetchJSON('/api/medical/score', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        score_type: 'gfr',
        params: {
          age: gfrForm.value.age,
          weight_kg: gfrForm.value.weight,
          serum_creatinine_umol_l: gfrForm.value.scr,
          sex: gfrForm.value.sex,
        },
      }),
    })
    gfrResult.value = res
  } catch (err) {
    alert('计算失败: ' + err.message)
  } finally {
    loadingCalc.value = false
  }
}

async function runCHADS() {
  loadingCalc.value = true
  try {
    const res = await fetchJSON('/api/medical/score', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        score_type: 'cha2ds2_vasc',
        params: {
          age: chadsForm.value.age,
          sex: chadsForm.value.sex,
          chf: chadsForm.value.chf,
          hypertension: chadsForm.value.hypertension,
          diabetes: chadsForm.value.diabetes,
          stroke_tia: chadsForm.value.stroke,
          vascular_disease: chadsForm.value.vascular,
        },
      }),
    })
    chadsResult.value = res
  } catch (err) {
    alert('评估失败: ' + err.message)
  } finally {
    loadingCalc.value = false
  }
}

function insertGFRToSOAP() {
  if (!gfrResult.value) return
  soapForm.value.objective += `\n【肾功能测定】eGFR(Cockcroft-Gault) = ${gfrResult.value.value} ml/min，处于 ${gfrResult.value.stage}。`
  alert('已自动将 eGFR 计算指标填入客观检查 (Objective) 栏目！')
}

function insertCHADSToSOAP() {
  if (!chadsResult.value) return
  soapForm.value.objective += `\n【卒中风险评估】CHA2DS2-VASc 评分 = ${chadsResult.value.value} 分，风险等级：${chadsResult.value.risk_category}。`
  alert('已将 CHA2DS2-VASc 卒中评分填入病历！')
}

function insertBMIToSOAP() {
  if (!bmiResult.value) return
  activePatient.value.vitals.bmi = bmiResult.value.value
  alert('已更新患者健康画像 BMI 指标！')
}

// 处方安全审计
async function runSafetyCheck() {
  loadingSafety.value = true
  try {
    const drugs = safetyForm.value.drugs.split(/[,，、\s]+/).filter(Boolean)
    const allergies = safetyForm.value.allergies.split(/[,，、\s]+/).filter(Boolean)
    const res = await fetchJSON('/api/medical/safety', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ drugs, allergies, conditions: [] }),
    })
    safetyResult.value = res
  } catch (err) {
    alert('处方安全审计失败: ' + err.message)
  } finally {
    loadingSafety.value = false
  }
}

// SOAP 生成与知识库沉淀
async function runGenerateSOAP() {
  loadingSoap.value = true
  try {
    const res = await fetchJSON('/api/medical/soap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        patient_info: { name: soapForm.value.patientName, age: soapForm.value.patientAge, gender: soapForm.value.patientGender },
        subjective: { chief_complaint: soapForm.value.chiefComplaint, history_present_illness: soapForm.value.hpi },
        objective: { physical_exam: soapForm.value.objective },
        assessment: { primary_diagnosis: currentAssessment.value },
        plan: { orders: [currentPlan.value] },
        save_document: false,
      }),
    })
    soapResultMarkdown.value = res.markdown
  } catch (err) {
    alert('病历生成失败: ' + err.message)
  } finally {
    loadingSoap.value = false
  }
}

async function saveSOAPToRAG() {
  savingDoc.value = true
  try {
    const res = await fetchJSON('/api/medical/soap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        patient_info: { name: soapForm.value.patientName, age: soapForm.value.patientAge, gender: soapForm.value.patientGender },
        subjective: { chief_complaint: soapForm.value.chiefComplaint, history_present_illness: soapForm.value.hpi },
        objective: { physical_exam: soapForm.value.objective },
        assessment: { primary_diagnosis: currentAssessment.value },
        plan: { orders: [currentPlan.value] },
        save_document: true,
      }),
    })
    alert('🎉 门诊 SOAP 电子病历已成功持久化并同步至 RAG 知识库！（文档ID: ' + (res.document_id || '已保存') + '）')
  } catch (err) {
    alert('同步失败: ' + err.message)
  } finally {
    savingDoc.value = false
  }
}

function copySOAPMarkdown() {
  const text = `# 门诊 SOAP 记录 - ${soapForm.value.patientName}\n\n## S 主诉与现病史\n- 主诉: ${soapForm.value.chiefComplaint}\n- 现病史: ${soapForm.value.hpi}\n\n## O 客观体征与检验\n- ${soapForm.value.objective}\n\n## A 临床评估\n- ${currentAssessment.value}\n\n## P 处置计划\n- ${currentPlan.value}`
  navigator.clipboard.writeText(text)
  alert('已复制病历 Markdown 到剪贴板！')
}
</script>

<style scoped>
/* 遮罩层 */
.medical-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.65);
  backdrop-filter: blur(8px);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  animation: fadeIn 0.2s ease-out;
}

.medical-overlay.fullscreen-mode {
  padding: 0;
}

.medical-shell {
  background: #ffffff;
  width: 100%;
  max-width: 1380px;
  height: 92vh;
  border-radius: 16px;
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgba(226, 232, 240, 0.8);
  transition: all 0.25s ease;
}

.fullscreen-mode .medical-shell {
  max-width: 100vw;
  height: 100vh;
  border-radius: 0;
}

/* Header */
.medical-header {
  padding: 12px 24px;
  background: #f8fafc;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
  gap: 16px;
}

.medical-brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.medical-logo {
  width: 36px;
  height: 36px;
  background: linear-gradient(135deg, #0284c7, #0369a1);
  color: #ffffff;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.medical-logo svg { width: 20px; height: 20px; }

.medical-kicker {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.05em;
  color: #0284c7;
  text-transform: uppercase;
}

.medical-header h1 {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
  margin: 0;
}

/* 核心视窗切换器 (Segmented Control) */
.view-mode-switcher {
  display: flex;
  background: #e2e8f0;
  padding: 4px;
  border-radius: 10px;
  gap: 4px;
}

.switch-btn {
  border: none;
  background: transparent;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  color: #475569;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.2s;
}

.switch-btn:hover { color: #0f172a; }
.switch-btn.active {
  background: #ffffff;
  color: #0284c7;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
}

.medical-header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.status-indicator {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  background: #ecfdf5;
  color: #059669;
  border: 1px solid #a7f3d0;
}

.status-indicator.emergency {
  background: #fef2f2;
  color: #dc2626;
  border-color: #fecaca;
}

.pulse-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
  animation: pulse 1.6s infinite;
}

.emergency .pulse-dot {
  animation: pulseRed 1s infinite;
}

@keyframes pulseRed {
  0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(220, 38, 38, 0.7); }
  70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(220, 38, 38, 0); }
  100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(220, 38, 38, 0); }
}

.icon-button {
  background: transparent;
  border: none;
  color: #64748b;
  cursor: pointer;
  padding: 6px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.icon-button:hover { background: #f1f5f9; color: #0f172a; }
.icon-button svg { width: 18px; height: 18px; }

/* S0 呼吸脉冲横幅 */
.emergency-pulse-banner {
  background: linear-gradient(90deg, #991b1b, #b91c1c);
  color: #ffffff;
  padding: 10px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
  box-shadow: 0 4px 12px rgba(185, 28, 28, 0.3);
}

.emergency-pulse-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.alarm-badge {
  background: #fee2e2;
  color: #991b1b;
  font-size: 11px;
  font-weight: 800;
  padding: 3px 8px;
  border-radius: 6px;
}

.alert-info strong { font-size: 14px; }
.alert-info p { font-size: 12px; opacity: 0.9; margin-top: 2px; }

.emergency-pulse-actions {
  display: flex;
  gap: 10px;
}

.emergency-action-btn {
  border: none;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}
.emergency-action-btn.primary { background: #ffffff; color: #991b1b; }
.emergency-action-btn.secondary { background: rgba(255, 255, 255, 0.2); color: #ffffff; border: 1px solid rgba(255,255,255,0.4); }

/* ============================================================== */
/* 视图 1: 旗舰级三栏工作台 (Workstation) */
/* ============================================================== */
.workstation-grid {
  display: grid;
  grid-template-columns: 310px 1fr 390px;
  flex: 1;
  overflow: hidden;
  background: #f1f5f9;
  gap: 1px;
}

.col-patient, .col-consultation, .col-cdss {
  overflow-y: auto;
  height: 100%;
}

.panel-card {
  background: #ffffff;
  height: 100%;
  display: flex;
  flex-direction: column;
}

.panel-header {
  padding: 12px 16px;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fafafa;
}

.panel-title {
  font-size: 13px;
  font-weight: 700;
  color: #1e293b;
}

.patient-id-badge {
  font-size: 11px;
  font-family: monospace;
  background: #e2e8f0;
  padding: 2px 6px;
  border-radius: 4px;
  color: #475569;
}

/* 患者画像卡片 */
.patient-profile {
  padding: 14px 16px;
  border-bottom: 1px solid #f1f5f9;
}

.profile-main {
  display: flex;
  align-items: center;
  gap: 12px;
}

.patient-avatar {
  font-size: 32px;
  width: 48px;
  height: 48px;
  background: #e0f2fe;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.name-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.p-name { font-size: 16px; font-weight: 700; color: #0f172a; }
.p-tag { font-size: 12px; color: #64748b; }
.p-dept { font-size: 11px; color: #0284c7; margin-top: 2px; }

.vitals-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-top: 12px;
  background: #f8fafc;
  padding: 8px;
  border-radius: 8px;
  text-align: center;
}

.vital-lbl { font-size: 10px; color: #64748b; display: block; }
.vital-val { font-size: 13px; font-weight: 700; color: #0f172a; }
.vital-val.warn { color: #dc2626; }

.allergy-strip {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.allergy-tag {
  background: #fee2e2;
  color: #b91c1c;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
}

/* 槽位雷达条 */
.slot-tracker-card {
  padding: 12px 16px;
  border-bottom: 1px solid #f1f5f9;
}

.slot-tracker-header {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  font-weight: 700;
  color: #334155;
  margin-bottom: 6px;
}

.slot-score { color: #0284c7; }

.progress-bar {
  height: 5px;
  background: #e2e8f0;
  border-radius: 999px;
  overflow: hidden;
  margin-bottom: 10px;
}
.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #38bdf8, #0284c7);
  border-radius: 999px;
  transition: width 0.3s ease;
}

.slot-chip-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}

.slot-chip {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 11px;
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  transition: all 0.2s;
}

.slot-chip:hover { border-color: #0284c7; background: #f0f9ff; }
.slot-chip.filled { background: #f0fdf4; border-color: #bbf7d0; color: #15803d; }
.chip-status { font-weight: 700; }

/* 典型病例库 */
.preset-cases-box {
  padding: 12px 16px;
  flex: 1;
}

.box-title {
  font-size: 11px;
  font-weight: 700;
  color: #64748b;
  margin-bottom: 8px;
  text-transform: uppercase;
}

.preset-case-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.preset-case-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  background: #ffffff;
  text-align: left;
  cursor: pointer;
  transition: all 0.2s;
}

.preset-case-btn:hover { background: #f8fafc; border-color: #cbd5e1; }
.preset-case-btn.active {
  border-color: #0284c7;
  background: #f0f9ff;
  box-shadow: 0 1px 3px rgba(2, 132, 199, 0.15);
}

.case-level {
  font-size: 10px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 4px;
}
.case-level.danger { background: #fee2e2; color: #991b1b; }
.case-level.warning { background: #fef3c7; color: #92400e; }
.case-level.info { background: #e0f2fe; color: #0369a1; }
.case-level.normal { background: #f1f5f9; color: #475569; }

.case-name {
  font-size: 12px;
  font-weight: 600;
  color: #1e293b;
  flex: 1;
}

/* 中栏：MDT 协同会诊流 */
.col-consultation .chat-card {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.badge-tag {
  font-size: 10px;
  background: #e0f2fe;
  color: #0284c7;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
}

.sm-btn {
  background: transparent;
  border: 1px solid #cbd5e1;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  color: #475569;
}
.sm-btn:hover { background: #f1f5f9; }

.consultation-stream {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: #f8fafc;
}

.mdt-message-bubble {
  display: flex;
  gap: 10px;
}

.mdt-message-bubble.user {
  flex-direction: row-reverse;
}

.agent-avatar {
  font-size: 24px;
  width: 36px;
  height: 36px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.content-cell {
  max-width: 82%;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 12px 14px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
}

.user .content-cell {
  background: #0284c7;
  color: #ffffff;
  border-color: #0284c7;
}

.sender-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.sender-name { font-size: 12px; font-weight: 700; color: #1e293b; }
.user .sender-name { color: #ffffff; }

.sender-role-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
}

.triage-badge { background: #dbeafe; color: #1d4ed8; }
.safety-badge { background: #fee2e2; color: #b91c1c; }
.specialist-badge { background: #fef3c7; color: #b45309; }
.doctor-badge { background: rgba(255, 255, 255, 0.2); color: #ffffff; }

.msg-time { font-size: 10px; color: #94a3b8; margin-left: auto; }
.user .msg-time { color: rgba(255, 255, 255, 0.8); }

.message-text {
  font-size: 13px;
  line-height: 1.6;
  color: #334155;
  white-space: pre-wrap;
  word-break: break-word;
}
.user .message-text { color: #ffffff; }

/* CoT 思维链折叠 */
.cot-trace-box {
  margin-top: 8px;
  background: #f1f5f9;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 11px;
}

.cot-trace-box summary {
  cursor: pointer;
  color: #0284c7;
  font-weight: 600;
  outline: none;
}

.cot-content {
  margin-top: 6px;
  font-family: monospace;
  font-size: 11px;
  line-height: 1.4;
  white-space: pre-wrap;
  color: #475569;
}

.inline-safety-alerts {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.safety-pill {
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 700;
}
.safety-pill.S0 { background: #fee2e2; color: #991b1b; }
.safety-pill.S1 { background: #fef3c7; color: #92400e; }

/* 医生人机审核 */
.doctor-approval-bar {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #e2e8f0;
  display: flex;
  gap: 8px;
}

.approve-btn, .sync-soap-btn {
  border: 1px solid #cbd5e1;
  background: #f8fafc;
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  color: #475569;
}

.approve-btn.approved {
  background: #dcfce7;
  color: #15803d;
  border-color: #86efac;
}

/* 快捷医嘱栏 */
.quick-order-bar {
  padding: 8px 16px;
  background: #fafafa;
  border-top: 1px solid #e2e8f0;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.quick-order-label { font-size: 11px; color: #64748b; font-weight: 600; }

.order-chip {
  background: #ffffff;
  border: 1px solid #cbd5e1;
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 11px;
  cursor: pointer;
  color: #334155;
  transition: all 0.2s;
}

.order-chip:hover { border-color: #0284c7; color: #0284c7; background: #f0f9ff; }

/* 底部输入框 */
.chat-input-row {
  padding: 12px 16px;
  background: #ffffff;
  border-top: 1px solid #e2e8f0;
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

.chat-textarea {
  flex: 1;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
  resize: none;
  font-family: inherit;
}
.chat-textarea:focus { outline: none; border-color: #0284c7; ring: 2px solid #bae6fd; }

.send-btn {
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  cursor: pointer;
  white-space: nowrap;
}

/* 右栏：CDSS 工具箱 & 实时 SOAP */
.col-cdss .cdss-card {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.cdss-tabs {
  display: flex;
  border-bottom: 1px solid #e2e8f0;
  background: #f8fafc;
}

.cdss-tabs button {
  flex: 1;
  border: none;
  background: transparent;
  padding: 10px;
  font-size: 12px;
  font-weight: 600;
  color: #64748b;
  cursor: pointer;
  border-bottom: 2px solid transparent;
}

.cdss-tabs button.active {
  color: #0284c7;
  border-bottom-color: #0284c7;
  background: #ffffff;
}

.cdss-body {
  flex: 1;
  overflow-y: auto;
  padding: 14px;
}

/* SOAP 看板 */
.soap-doc-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.soap-title { font-size: 13px; font-weight: 700; color: #1e293b; }
.soap-actions { display: flex; gap: 6px; }

.icon-text-btn {
  border: 1px solid #cbd5e1;
  background: #ffffff;
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
}
.icon-text-btn.primary { background: #0284c7; color: #ffffff; border-color: #0284c7; }

.soap-sections {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.soap-sec {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
  background: #f8fafc;
}

.sec-tag {
  font-size: 11px;
  font-weight: 700;
  margin-bottom: 6px;
  padding-bottom: 4px;
  border-bottom: 1px solid #e2e8f0;
}

.s-sec .sec-tag { color: #0284c7; }
.o-sec .sec-tag { color: #16a34a; }
.a-sec .sec-tag { color: #d97706; }
.p-sec .sec-tag { color: #7c3aed; }

.sec-content {
  font-size: 12px;
  line-height: 1.5;
  color: #334155;
}

.red-alert-text {
  color: #dc2626;
  font-weight: 700;
  margin-top: 4px;
}

/* 临床微计算器卡片 */
.micro-calc-box {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
}

.micro-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  font-weight: 700;
  color: #1e293b;
  margin-bottom: 8px;
}

.micro-run-btn {
  border: none;
  background: #0284c7;
  color: #ffffff;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
}

.micro-inputs {
  display: flex;
  gap: 6px;
}

.micro-inputs input {
  flex: 1;
  padding: 4px 6px;
  border: 1px solid #cbd5e1;
  border-radius: 4px;
  font-size: 11px;
}

.micro-result {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.res-num { font-weight: 700; color: #0284c7; }
.res-badge { background: #e0f2fe; color: #0369a1; padding: 1px 6px; border-radius: 4px; font-size: 10px; }
.res-badge.warn { background: #fee2e2; color: #991b1b; }

.insert-btn {
  margin-left: auto;
  border: 1px solid #0284c7;
  background: #ffffff;
  color: #0284c7;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
  cursor: pointer;
}

.chads-checks {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  font-size: 11px;
}

/* DDI 审计 */
.ddi-panel-box {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px;
}

.sm-label { font-size: 11px; color: #475569; display: block; margin: 6px 0 2px 0; }
.full-input { width: 100%; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 12px; }
.full-btn { width: 100%; margin-top: 10px; padding: 6px 0; font-size: 12px; }

.audit-status {
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 700;
  margin-top: 10px;
}
.audit-status.safe { background: #dcfce7; color: #166534; }
.audit-status.danger { background: #fee2e2; color: #991b1b; }

.ddi-conflict-item {
  margin-top: 8px;
  background: #ffffff;
  border: 1px solid #fecaca;
  padding: 8px;
  border-radius: 6px;
}

.conf-head { display: flex; justify-content: space-between; font-size: 12px; font-weight: 700; color: #991b1b; }
.conf-desc { font-size: 11px; color: #475569; margin-top: 4px; }

/* ============================================================== */
/* 视图 2: 多专家会诊流全屏 (HuatuoGPT & AI-Doctor) */
/* ============================================================== */
.consultation-full-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #f8fafc;
}

.consult-top-bar {
  padding: 10px 24px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.patient-pill-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #1e293b;
}

.p-divider { color: #cbd5e1; }

.slots-inline-tracker {
  display: flex;
  align-items: center;
  gap: 6px;
}

.tracker-title { font-size: 11px; color: #64748b; font-weight: 600; }

.slot-pill {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #f1f5f9;
  font-size: 11px;
  color: #64748b;
}

.slot-pill .dot { width: 6px; height: 6px; border-radius: 50%; background: #94a3b8; }
.slot-pill.filled { background: #dcfce7; color: #166534; font-weight: 600; }
.slot-pill.filled .dot { background: #16a34a; }

.consultation-flow-container {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
}

.flow-message-list {
  max-width: 960px;
  width: 100%;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.flow-msg-row {
  display: flex;
  gap: 12px;
}

.flow-msg-row.user {
  flex-direction: row-reverse;
}

.msg-avatar-badge {
  font-size: 28px;
  width: 44px;
  height: 44px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.msg-body-wrapper {
  max-width: 80%;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 14px 18px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}

.user .msg-body-wrapper {
  background: #0284c7;
  color: #ffffff;
}

.msg-meta-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.msg-meta-line .name { font-size: 13px; font-weight: 700; color: #1e293b; }
.user .msg-meta-line .name { color: #ffffff; }

.role-badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 600;
}

.msg-bubble {
  font-size: 13.5px;
  line-height: 1.65;
  color: #334155;
  white-space: pre-wrap;
}
.user .msg-bubble { color: #ffffff; }

.cot-flow-box {
  margin-top: 10px;
  background: #f8fafc;
  border-radius: 6px;
  padding: 8px 12px;
}
.cot-flow-box summary { cursor: pointer; color: #0284c7; font-weight: 600; font-size: 12px; }
.cot-text { margin-top: 6px; font-family: monospace; font-size: 11px; white-space: pre-wrap; color: #475569; }

.doctor-action-btns {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid #f1f5f9;
  display: flex;
  gap: 8px;
}

.doc-btn {
  border: 1px solid #cbd5e1;
  background: #ffffff;
  padding: 3px 10px;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  color: #475569;
}
.doc-btn.done { background: #dcfce7; color: #15803d; border-color: #86efac; }
.doc-btn.soap { color: #0284c7; border-color: #0284c7; }

/* 底部停靠栏 */
.consultation-bottom-dock {
  background: #ffffff;
  border-top: 1px solid #e2e8f0;
  padding: 12px 24px;
}

.quick-pill-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.dock-label { font-size: 11px; color: #64748b; font-weight: 600; }

.pill-btn {
  background: #f1f5f9;
  border: 1px solid #e2e8f0;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11px;
  cursor: pointer;
  color: #334155;
  transition: all 0.2s;
}
.pill-btn:hover { background: #e0f2fe; color: #0284c7; border-color: #bae6fd; }

.flow-input-bar {
  display: flex;
  gap: 12px;
  align-items: flex-end;
}

.flow-input {
  flex: 1;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 13px;
  resize: none;
  font-family: inherit;
}
.flow-input:focus { outline: none; border-color: #0284c7; }

.flow-send-btn {
  padding: 10px 24px;
  font-size: 14px;
  font-weight: 700;
  border-radius: 8px;
  white-space: nowrap;
}

/* ============================================================== */
/* 视图 3: 独立工具箱 (Toolbox) */
/* ============================================================== */
.toolbox-view {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.sub-tab-nav {
  display: flex;
  padding: 0 24px;
  background: #f8fafc;
  border-bottom: 1px solid #e2e8f0;
}

.sub-tab-nav button {
  border: none;
  background: transparent;
  padding: 12px 20px;
  font-size: 13px;
  font-weight: 600;
  color: #64748b;
  cursor: pointer;
  border-bottom: 2px solid transparent;
}

.sub-tab-nav button.active {
  color: #0284c7;
  border-bottom-color: #0284c7;
  background: #ffffff;
}

.toolbox-body {
  padding: 24px;
  flex: 1;
  overflow-y: auto;
}

.calc-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 20px;
}

.calc-card {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 18px;
}

.calc-card h3 { font-size: 15px; font-weight: 700; color: #0f172a; margin: 0 0 6px 0; }
.calc-desc { font-size: 12px; color: #64748b; margin-bottom: 14px; }

.form-row { display: flex; gap: 12px; margin-bottom: 12px; }
.form-row label { flex: 1; font-size: 12px; font-weight: 600; color: #334155; }
.form-row input, .form-row select { width: 100%; margin-top: 4px; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 13px; }

.chads-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 14px; font-size: 12px; }

.result-box {
  margin-top: 14px;
  padding: 12px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
}

.result-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.result-header .val { font-size: 18px; font-weight: 800; color: #0f172a; }
.result-header .badge { font-size: 11px; padding: 2px 8px; border-radius: 4px; font-weight: 700; }

.badge-success { background: #dcfce7; color: #166534; }
.badge-warning { background: #fef3c7; color: #92400e; }
.badge-danger { background: #fee2e2; color: #991b1b; }
.stage-tag { background: #e0f2fe; color: #0284c7; font-weight: 700; padding: 2px 8px; border-radius: 4px; font-size: 11px; }

.advice { font-size: 12px; color: #475569; line-height: 1.5; }

.safety-card-full { max-width: 600px; margin: 0 auto; }
.ddi-box { margin-top: 16px; }
.status-banner { padding: 10px; border-radius: 6px; font-weight: 700; font-size: 13px; margin-bottom: 8px; }
.banner-safe { background: #dcfce7; color: #166534; }
.banner-s0 { background: #fee2e2; color: #991b1b; }

.soap-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.soap-input-card, .soap-output-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 18px; }
.markdown-preview { margin-top: 10px; padding: 14px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; font-family: monospace; font-size: 12px; line-height: 1.6; white-space: pre-wrap; max-height: 480px; overflow-y: auto; }

/* 通用按钮 */
.btn.primary {
  background: #0284c7;
  color: #ffffff;
  border: none;
  cursor: pointer;
}
.btn.primary:hover { background: #0369a1; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* 动画 */
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
.slide-down-enter-active, .slide-down-leave-active { transition: all 0.3s ease; }
.slide-down-enter-from, .slide-down-leave-to { transform: translateY(-100%); opacity: 0; }
</style>
