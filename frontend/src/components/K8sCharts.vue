<script setup>
// K8s 总览图表区：操作趋势/资源规模/集群状态/Pod 分布/负载类型 + 证书到期。
// 图表思路吸收自 k8smanager 看板；证书只统计 K8s 集群内部 pki 组件证书。
import { BarChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import MetricChart from '@/components/MetricChart.vue'
import { isDark } from '@/composables/theme'
import { t } from '@/i18n'

echarts.use([BarChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  charts: { type: Object, default: () => ({}) },
})

const COLORS = ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#9a6fe0', '#13c2c2', '#fa8c16', '#52c1a']
const scaleEl = ref(null)
const clusterEl = ref(null)
const podEl = ref(null)
const wlEl = ref(null)
const chartsInst = []

function axisColors() {
  const dark = isDark.value
  return {
    text: dark ? '#8b949e' : '#606266',
    split: dark ? 'rgba(139,148,158,0.15)' : 'rgba(0,0,0,0.06)',
  }
}

function donut(el, title, data, centerLabel) {
  if (!el) return
  let inst = echarts.getInstanceByDom(el) || echarts.init(el)
  chartsInst.push(inst)
  const { text } = axisColors()
  inst.setOption({
    color: COLORS,
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, textStyle: { color: text, fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['52%', '72%'], center: ['50%', '44%'],
      label: { show: true, formatter: '{b}: {c}', color: text, fontSize: 11 },
      data: data.length ? data : [{ name: centerLabel || t('k8s.chart.noData'), value: 0 }],
    }],
  }, true)
}

function bar(el, data) {
  if (!el) return
  let inst = echarts.getInstanceByDom(el) || echarts.init(el)
  chartsInst.push(inst)
  const { text, split } = axisColors()
  const items = [...data].sort((a, b) => b.count - a.count)
  inst.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 100, right: 24, top: 10, bottom: 24 },
    xAxis: { type: 'value', axisLabel: { color: text }, splitLine: { lineStyle: { color: split } } },
    yAxis: {
      type: 'category', data: items.map((i) => i.kind),
      axisLabel: { color: text, fontSize: 11 },
    },
    series: [{ type: 'bar', data: items.map((i) => i.count), barMaxWidth: 18, itemStyle: { color: '#409eff', borderRadius: [0, 6, 6, 0] } }],
  }, true)
}

function render() {
  const c = props.charts || {}
  bar(scaleEl.value, c.scale || [])
  donut(clusterEl.value, t('k8s.chart.clusterStatus'), (c.cluster_status || []).map((x) => ({ name: x.status, value: x.count })), t('k8s.chart.noCluster'))
  donut(podEl.value, t('k8s.chart.podStatus'), (c.pod_status || []).map((x) => ({ name: x.status, value: x.count })), t('k8s.chart.noPod'))
  donut(wlEl.value, t('k8s.chart.workload'), (c.workload_types || []).map((x) => ({ name: x.kind, value: x.count })), t('k8s.chart.noWorkload'))
}

function dayColor(d) {
  if (d < 0) return 'danger'
  if (d <= 30) return 'danger'
  if (d <= 90) return 'warning'
  return 'success'
}

onMounted(() => {
  render()
  window.addEventListener('resize', render)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', render)
  chartsInst.forEach((i) => i.dispose())
})
watch(() => props.charts, () => render(), { deep: true })
</script>

<template>
  <div class="charts-wrap">
    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never" class="chart-card">
          <MetricChart :title="t('k8s.chart.trendTitle')" :series="[
            { name: t('k8s.chart.opsCount'), points: (charts.trend || []).map((p) => [p.date, p.total]) },
            { name: t('k8s.chart.failCount'), points: (charts.trend || []).map((p) => [p.date, p.failed]) },
          ]" :height="230" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never" class="chart-card">
          <div class="chart-title">{{ t('k8s.chart.scaleTitle', { scope: charts.scope || t('k8s.chart.scopeAll') }) }}</div>
          <div ref="scaleEl" class="chart-box" />
        </el-card>
      </el-col>
    </el-row>
    <el-row :gutter="16" class="row2">
      <el-col :span="8">
        <el-card shadow="never" class="chart-card">
          <div class="chart-title">{{ t('k8s.chart.clusterTitle', { scope: charts.scope || t('k8s.chart.scopeAll') }) }}</div>
          <div ref="clusterEl" class="chart-box" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="chart-card">
          <div class="chart-title">{{ t('k8s.chart.podStatusDist') }}</div>
          <div ref="podEl" class="chart-box" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="chart-card">
          <div class="chart-title">{{ t('k8s.chart.workloadTypes') }}</div>
          <div ref="wlEl" class="chart-box" />
        </el-card>
      </el-col>
    </el-row>
    <el-card shadow="never" class="chart-card cert-card">
      <div class="chart-title">{{ t('k8s.chart.certTitle') }}</div>
      <div v-if="(charts.cert_soon || []).length" class="cert-list">
        <div v-for="c in charts.cert_soon" :key="c.name" class="cert-row">
          <span class="cert-name">{{ c.name }}</span>
          <span class="tip">{{ t('k8s.chart.expireAt', { product: c.product, time: c.expire_time }) }}</span>
          <el-tag size="small" :type="dayColor(c.days_left)" effect="plain">
            {{ c.days_left < 0 ? t('k8s.chart.expiredDays', { n: -c.days_left }) : t('k8s.chart.leftDays', { n: c.days_left }) }}
          </el-tag>
        </div>
      </div>
      <div v-else class="tip">{{ t('k8s.chart.noCertExpiring') }}</div>
    </el-card>
  </div>
</template>

<style scoped>
.charts-wrap { display: flex; flex-direction: column; gap: 16px; }
.row2 { margin-top: 0; }
.chart-card { border-radius: 12px; }
.chart-title { font-size: 13.5px; font-weight: 600; margin-bottom: 8px; }
.chart-box { height: 230px; }
.cert-card .cert-list { display: flex; flex-direction: column; gap: 8px; }
.cert-row { display: flex; align-items: center; gap: 12px; padding: 6px 2px; border-bottom: 1px solid var(--el-border-color-lighter); }
.cert-row:last-child { border-bottom: none; }
.cert-name { font-weight: 600; min-width: 220px; }
.tip { font-size: 12px; color: var(--el-text-color-secondary); }
</style>
