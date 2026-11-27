<script setup>
// 诊断报告子组件
import MarkdownIt from 'markdown-it'
import { t } from '@/i18n'

const props = defineProps({
  report: { type: Object, required: true },   // 诊断会话或案例详情（steps/conclusion_md/level/...）
})

const md = new MarkdownIt({ html: false, breaks: true })
const LEVEL_LABELS = {
  critical: 'diagnose.level.critical', warning: 'diagnose.level.warning',
  ok: 'diagnose.level.ok', unknown: 'diagnose.level.unknown',
}

const time = () => (props.report.diagnosed_at || props.report.created_at || '').replace('T', ' ').slice(0, 19)
</script>

<template>
  <div>
    <!-- 排查轨迹 -->
    <div class="section-title">{{ t('diagnose.report.trail') }}</div>
    <el-timeline style="padding-left: 4px; margin-top: 8px">
      <el-timeline-item v-for="s in report.steps || []" :key="s.step"
                        :type="s.error ? 'danger' : (s.tool ? 'primary' : 'success')"
                        :timestamp="s.duration_ms ? s.duration_ms + ' ms' : ''">
        <div>
          <el-tag v-if="s.tool" size="small" effect="plain" style="margin-right: 8px">{{ s.tool }}</el-tag>
          <span v-else style="color: var(--el-color-success); font-weight: 600">{{ t('diagnose.report.final_conclusion') }}</span>
          <span style="font-size: 13px">{{ s.summary }}</span>
        </div>
        <div class="sub" style="margin: 4px 0 0">{{ s.thought }}</div>
        <div v-if="s.params && Object.keys(s.params).length" class="sub" style="margin-top: 2px">
          {{ t('diagnose.report.params') }}{{ JSON.stringify(s.params) }}
        </div>
      </el-timeline-item>
    </el-timeline>

    <!-- 复盘信息（案例才有） -->
    <template v-if="report.root_cause !== undefined">
      <div class="section-title" style="margin-top: 18px">{{ t('diagnose.report.review') }}</div>
      <el-descriptions :column="1" border size="small" style="margin-top: 8px">
        <el-descriptions-item :label="t('diagnose.report.root_cause')">{{ report.root_cause || t('diagnose.report.not_filled') }}</el-descriptions-item>
        <el-descriptions-item :label="t('diagnose.report.solution')">{{ report.solution || t('diagnose.report.not_filled') }}</el-descriptions-item>
        <el-descriptions-item :label="t('diagnose.report.tags')">
          <el-tag v-for="tag in (report.tags || '').split(',').filter(Boolean)" :key="tag"
                  size="small" effect="plain" style="margin-right: 6px">{{ tag }}</el-tag>
          <span v-if="!report.tags" class="sub">{{ t('diagnose.report.none') }}</span>
        </el-descriptions-item>
      </el-descriptions>
    </template>

    <!-- 结论文本 -->
    <div class="section-title" style="margin-top: 18px">{{ t('diagnose.report.conclusion') }}</div>
    <div class="markdown" v-html="md.render(report.conclusion_md || t('diagnose.report.no_conclusion'))" />
  </div>
</template>

<script>
export default { name: 'DiagReport' }
</script>

<style scoped>
.section-title {
  font-weight: 600;
  border-left: 3px solid var(--el-color-primary);
  padding-left: 8px;
}
.sub {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.markdown :deep(h2) {
  font-size: 15px;
  margin: 14px 0 8px;
}
.markdown :deep(p) {
  margin: 6px 0;
  line-height: 1.7;
}
.markdown :deep(ol), .markdown :deep(ul) {
  padding-left: 20px;
  margin: 6px 0;
}
.markdown :deep(li) {
  margin: 3px 0;
  line-height: 1.6;
}
.markdown :deep(strong) {
  color: var(--el-color-primary);
}
</style>
