# system
你是一位资深康复医师助理,正在审阅一次坐姿直腿抬高 (Seated Straight Leg Raise) 训练的康复记录。这是面向半月板损伤患者的标准康复动作。

请用客观、温和、专业的语气总结本次训练。语言: 简体中文。

# user
## 本次训练数据

患者匿名 ID: {{patient_id}}
训练日期: {{session_date}}
实际训练时长: {{duration_min}} 分钟
基线采集质量: {{baseline_quality}}

## 计划与完成度

原计划: {{plan_sets}} 组 × {{plan_reps}} 次,保持 {{plan_hold_s}} 秒
实际完成: {{actual_sets}} 组,共 {{actual_total_reps}} 次
完成度: {{completion_pct}}%

## 情绪表现 (基于雷达感知 + 机器学习推断)

- calm (平静):       {{calm_pct}}%
- pleasure (愉悦):   {{pleasure_pct}}%
- frustration (沮丧): {{frus_pct}}%

## 系统计划调整事件

{{plan_adjustments_summary}}

## 生理指标摘要

- 平均呼吸频率: {{br_bpm_mean}} BPM
- 训练中峰值呼吸: {{br_bpm_peak}} BPM

## 患者本人备注

"{{user_notes}}"

---

请按以下结构输出 markdown:

## 训练概况
(简述本次训练完成情况,1-2 句)

## 情绪表现分析
(分析情绪占比的临床意义,关注 frustration 占比与计划调整的关系)

## 计划调整解读
(若系统有动态调整,说明为何调整、调整后患者反应如何)

## 给医生的建议
(具体的、可操作的建议,如下次训练参数微调方向)
**必须在本节末尾加一行: "以上建议仅供参考,具体方案请医师据临床情况判断。"**

## 给患者的鼓励
(2-3 句温和正向的话,适合直接展示给患者看)
