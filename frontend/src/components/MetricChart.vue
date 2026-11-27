<script setup>
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, MarkPointComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { isDark } from '@/composables/theme'

echarts.use([LineChart, GridComponent, LegendComponent, MarkPointComponent, TitleComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  title: { type: String, required: true },
  unit: { type: String, default: '' },
  series: { type: Array, default: () => [] }, // [{name, points: [[ts, val]]}]
  height: { type: Number, default: 240 },
  marks: { type: Array, default: () => [] },  // [{ts, value, label}] 标注异动等事件点
})

const el = ref(null)
let chart = null

function buildOption() {
  const dark = isDark.value
  const axisColor = dark ? '#8b949e' : '#606266'   // 画布内必须用字面色值，CSS 变量不显色
  const splitColor = dark ? 'rgba(139,148,158,0.15)' : 'rgba(0,0,0,0.06)'
  const colors = ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#9a6fe0', '#13c2c2']

  const series = props.series.map((s) => ({
    name: s.name || '',
    type: 'line',
    showSymbol: false,
    smooth: true,
    lineStyle: { width: 1.5 },
    data: s.points || [],
  }))
  // 异动标注：钉在首条曲线上（趋势标注）
  if (props.marks.length && series.length) {
    series[0].markPoint = {
      symbol: 'pin',
      symbolSize: 46,
      itemStyle: { color: '#f56c6c' },
      label: { fontSize: 9, color: '#fff', formatter: (p) => p.data.label },
      data: props.marks.map((m) => ({
        coord: [m.ts, m.value],
        label: m.label,
        value: m.value,
      })),
    }
  }

  return {
    color: colors,
    title: {
      text: props.unit ? `${props.title} (${props.unit})` : props.title,
      left: 8,
      top: 6,
      textStyle: { fontSize: 13, fontWeight: 500, color: dark ? '#e6e6e6' : '#303133' },
    },
    tooltip: {
      trigger: 'axis',
      confine: true,
      axisPointer: { type: 'cross', label: { backgroundColor: '#6a7985' } },
    },
    legend: {
      show: series.length > 1,
      top: 6,
      right: 8,
      icon: 'roundRect',
      itemWidth: 14,
      textStyle: { fontSize: 11, color: axisColor },
    },
    grid: { left: 56, right: 16, top: 40, bottom: 28 },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: axisColor } },
      axisLabel: { color: axisColor, fontSize: 10, hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: {
        color: axisColor,
        fontSize: 10,
        formatter: (v) => (v >= 10000 ? `${(v / 1000).toFixed(0)}k` : v),
      },
      splitLine: { lineStyle: { color: splitColor } },
    },
    series,
  }
}

function render() {
  if (!chart) return
  chart.setOption(buildOption(), true)
}

onMounted(() => {
  chart = echarts.init(el.value)
  render()
  chart.resize()
})

watch(() => props.series, render, { deep: true })
watch(() => props.marks, render, { deep: true })
watch(isDark, render)

// 容器尺寸变化时重绘
const ro = new ResizeObserver(() => chart?.resize())
onMounted(() => ro.observe(el.value))
onBeforeUnmount(() => {
  ro.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="el" class="metric-chart" :style="{ height: height + 'px' }" />
</template>

<style scoped>
.metric-chart {
  width: 100%;
}
</style>
