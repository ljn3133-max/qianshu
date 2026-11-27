<script setup>
import { PieChart } from 'echarts/charts'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { isDark } from '@/composables/theme'

echarts.use([PieChart, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  rows: { type: Array, default: () => [] }, // [{name, value}]
})

const el = ref(null)
let chart = null

function render() {
  if (!chart) return
  const dark = isDark.value
  chart.setOption({
    color: ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#9a6fe0', '#13c2c2', '#ff85c0', '#ffa940', '#bfbfbf'],
    tooltip: {
      trigger: 'item',
      confine: true,
      formatter: '{b}: ¥{c}（{d}%）',
      textStyle: { fontSize: 12 },
    },
    legend: {
      type: 'scroll',
      orient: 'vertical',
      right: 0,
      top: 'middle',
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { fontSize: 11, color: dark ? '#8b949e' : '#606266' },
      pageIconColor: dark ? '#8b949e' : '#606266',
      pageTextStyle: { color: dark ? '#8b949e' : '#606266' },
    },
    series: [{
      type: 'pie',
      radius: ['42%', '68%'],
      center: ['35%', '52%'],
      label: { show: false },
      itemStyle: { borderColor: dark ? '#1e1e1e' : '#fff', borderWidth: 1 },
      data: props.rows,
    }],
  }, true)
}

onMounted(() => {
  chart = echarts.init(el.value)
  render()
})
watch(() => props.rows, render, { deep: true })
watch(isDark, render)
onBeforeUnmount(() => {
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="el" style="height: 240px; width: 100%" />
</template>
