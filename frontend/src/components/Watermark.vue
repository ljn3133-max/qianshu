<script setup>
// 全局水印：用户+IP 平铺，用于截屏外流溯源。
import { computed } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const auth = useAuthStore()
const text = computed(() =>
  `${auth.user?.display_name || auth.user?.username || t('layout.brand')} · ${location.hostname}`)
const svg = computed(() => {
  const t = encodeURIComponent(text.value)
  return `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='460' height='280'%3E%3Ctext x='10' y='150' font-size='13' fill='rgba(120,130,150,0.055)' transform='rotate(-22 230 140)'%3E${t}%3C/text%3E%3C/svg%3E")`
})
</script>

<template>
  <div class="wm" :style="{ backgroundImage: svg }" />
</template>

<style scoped>
.wm {
  position: fixed; inset: 0; z-index: 9999; pointer-events: none;
  background-repeat: repeat;
}
</style>
