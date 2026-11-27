<script setup>
import { onMounted, reactive, ref } from 'vue'

import http from '@/api/http'
import { t } from '@/i18n'

// 滑块拼图验证码：纯前端按 seed 生成拼图，
// 后端只校验一次性票据。缺口位派生公式与后端一致：
// notch_x = min_x + seed % range（见 backend/app/api/v1/auth.py）
const emit = defineEmits(['success'])

const W = 320
const H = 160
const PIECE = 48

const st = reactive({
  challenge_id: '',
  seed: 0,
  min_x: 30,
  range: 200,
  tolerance: 6,
})
const bgCanvas = ref(null)
const pieceCanvas = ref(null)
const offsetX = ref(0)
const dragging = ref(false)
const passed = ref(false)
const tip = ref(t('login.captcha.slider_tip'))

let dragStartX = 0
let dragStartOffset = 0

function notchX() {
  return st.min_x + (st.seed % st.range)
}

function drawPuzzle() {
  const bg = bgCanvas.value
  const pc = pieceCanvas.value
  if (!bg || !pc) return
  const ctx = bg.getContext('2d')
  let s = st.seed || 1
  const rand = () => {
    s = (s * 1103515245 + 12345) % 2147483648
    return s / 2147483648
  }
  const g = ctx.createLinearGradient(0, 0, W, H)
  g.addColorStop(0, `hsl(${Math.floor(rand() * 360)}, 55%, 72%)`)
  g.addColorStop(1, `hsl(${Math.floor(rand() * 360)}, 55%, 58%)`)
  ctx.fillStyle = g
  ctx.fillRect(0, 0, W, H)
  for (let i = 0; i < 40; i++) {
    ctx.fillStyle = `rgba(255,255,255,${0.06 + rand() * 0.2})`
    ctx.beginPath()
    ctx.arc(rand() * W, rand() * H, 3 + rand() * 14, 0, Math.PI * 2)
    ctx.fill()
  }
  const nx = notchX()
  const ny = (H - PIECE) / 2
  pc.width = PIECE
  pc.height = PIECE
  const pctx = pc.getContext('2d')
  pctx.drawImage(bg, nx, ny, PIECE, PIECE, 0, 0, PIECE, PIECE)
  pctx.strokeStyle = 'rgba(255,255,255,0.9)'
  pctx.lineWidth = 2
  pctx.strokeRect(1, 1, PIECE - 2, PIECE - 2)
  ctx.fillStyle = 'rgba(0,0,0,0.32)'
  ctx.fillRect(nx, ny, PIECE, PIECE)
  ctx.strokeStyle = 'rgba(255,255,255,0.6)'
  ctx.lineWidth = 2
  ctx.strokeRect(nx, ny, PIECE, PIECE)
}

async function loadChallenge() {
  passed.value = false
  offsetX.value = 0
  tip.value = t('login.captcha.slider_tip')
  try {
    const r = await http.get('/auth/slider/challenge')
    if (!r.enabled) {
      tip.value = t('login.captcha.no_verify')
      return
    }
    st.challenge_id = r.challenge_id
    st.seed = r.seed
    st.min_x = r.min_x
    st.range = r.range
    st.tolerance = r.tolerance
    drawPuzzle()
  } catch {
    tip.value = t('login.captcha.load_failed')
  }
}
onMounted(loadChallenge)

function onDown(e) {
  if (passed.value || !st.challenge_id) return
  dragging.value = true
  dragStartX = (e.touches ? e.touches[0].clientX : e.clientX)
  dragStartOffset = offsetX.value
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
  window.addEventListener('touchmove', onMove, { passive: false })
  window.addEventListener('touchend', onUp)
}

function onMove(e) {
  if (!dragging.value) return
  if (e.cancelable) e.preventDefault()
  const x = (e.touches ? e.touches[0].clientX : e.clientX)
  offsetX.value = Math.max(0, Math.min(W - PIECE, dragStartOffset + (x - dragStartX)))
}

function onUp() {
  if (!dragging.value) return
  dragging.value = false
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseup', onUp)
  window.removeEventListener('touchmove', onMove)
  window.removeEventListener('touchend', onUp)
  submit()
}

async function submit() {
  const offset = Math.round(offsetX.value)
  if (Math.abs(offset - notchX()) > st.tolerance) {
    tip.value = t('login.captcha.miss')
    setTimeout(loadChallenge, 350)
    return
  }
  try {
    const r = await http.post('/auth/slider/verify', {
      challenge_id: st.challenge_id,
      offset,
    })
    passed.value = true
    tip.value = t('login.captcha.passed')
    emit('success', r.ticket)
  } catch {
    tip.value = t('login.captcha.failed')
    setTimeout(loadChallenge, 350)
  }
}
</script>

<template>
  <div class="slide-captcha" @click.self="loadChallenge">
    <div class="slide-stage">
      <canvas ref="bgCanvas" :width="320" :height="160" class="slide-bg"></canvas>
      <canvas
        ref="pieceCanvas"
        class="slide-piece"
        :class="{ 'slide-piece-drag': dragging, 'slide-piece-ok': passed }"
        :style="{ left: offsetX + 'px' }"
      ></canvas>
    </div>
    <div class="slide-track" :class="{ 'slide-track-ok': passed }">
      <div class="slide-bar" :style="{ width: (offsetX + 24) + 'px' }"></div>
      <div
        class="slide-handle"
        :class="{ 'slide-handle-ok': passed }"
        :style="{ left: offsetX + 'px' }"
        @mousedown.prevent="onDown"
        @touchstart.prevent="onDown"
      >
        {{ passed ? '✓' : '»' }}
      </div>
      <div class="slide-tip">{{ tip }}</div>
    </div>
  </div>
</template>

<style scoped>
.slide-captcha { width: 320px; user-select: none; }
.slide-stage {
  position: relative;
  width: 320px;
  height: 160px;
  border-radius: 8px;
  overflow: hidden;
}
.slide-bg { display: block; width: 320px; height: 160px; }
.slide-piece {
  position: absolute;
  top: 56px;
  width: 48px;
  height: 48px;
  border-radius: 6px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
}
.slide-piece-drag { box-shadow: 0 4px 12px rgba(0, 0, 0, 0.45); }
.slide-piece-ok { opacity: 0.85; }
.slide-track {
  position: relative;
  margin-top: 10px;
  height: 36px;
  border-radius: 18px;
  background: var(--el-fill-color-light, #eef2f7);
  overflow: hidden;
}
.slide-track-ok { background: var(--el-color-success-light-9, #e8f6ee); }
.slide-bar {
  position: absolute;
  left: 0;
  top: 0;
  height: 36px;
  border-radius: 18px;
  background: var(--el-color-primary-light-7, #b3d4ff);
}
.slide-handle {
  position: absolute;
  top: 2px;
  width: 32px;
  height: 32px;
  border-radius: 16px;
  background: var(--el-color-primary, #409eff);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  cursor: grab;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.25);
}
.slide-handle:active { cursor: grabbing; }
.slide-handle-ok { background: var(--el-color-success, #67c23a); }
.slide-tip {
  position: absolute;
  left: 44px;
  right: 8px;
  top: 0;
  height: 36px;
  line-height: 36px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: center;
  pointer-events: none;
}
</style>
