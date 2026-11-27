<script setup>
// 语言切换：右上角控制钮，默认中文，localStorage 持久化
import { locale, setLocale } from '@/i18n'

function onChange(v) {
  if (v !== locale.value) setLocale(v)
}
</script>

<template>
  <el-segmented
    :model-value="locale"
    :options="[{ label: '中', value: 'zh' }, { label: 'EN', value: 'en' }]"
    size="small"
    class="lang-switch"
    @change="onChange"
  />
</template>

<style scoped>
.lang-switch {
  --el-segmented-item-selected-bg-color: var(--el-color-primary);
  --el-segmented-item-selected-color: #fff;
}
</style>
