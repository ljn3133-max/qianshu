<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { useAuthStore } from '@/stores/auth'
import http from '@/api/http'
import SlideCaptcha from '@/components/SlideCaptcha.vue'
import { isDark, setTheme, themeMode, toggleTheme } from '@/composables/theme'
import { t } from '@/i18n'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const form = reactive({ username: '', password: '', captcha: '', otp: '', sliderTicket: '' })
const captcha = ref({ enabled: false, id: '', svg: '' })
const captchaMode = ref('slider')   // 人机验证形态：slider 滑块（默认）/ svg 文字（可切换）
const sliderKey = ref(0)            // 登录失败后重挂载=换新挑战
const needOtp = ref(false)
const otpEnabledHint = ref(false)   // 登录失败提示需 OTP 时才显示输入框
const recoveryHint = ref('')        // 找回兜底提示（后端判定：本机来源+连错 ≥3 次才下发文本）

async function loadCaptcha() {
  try {
    const r = await http.get('/auth/captcha')
    if (r.enabled) {
      captcha.value = { enabled: true, id: r.id, svg: r.svg }
    } else {
      captcha.value = { enabled: false, id: '', svg: '' }
    }
  } catch { captcha.value = { enabled: false, id: '', svg: '' } }
}
onMounted(loadCaptcha)
const loading = ref(false)

function onSliderOk(ticket) {
  form.sliderTicket = ticket
}

function switchCaptchaMode() {
  captchaMode.value = captchaMode.value === 'slider' ? 'svg' : 'slider'
  form.sliderTicket = ''
  form.captcha = ''
  sliderKey.value += 1
  loadCaptcha()
}

function resetCaptcha() {
  form.sliderTicket = ''
  form.captcha = ''
  sliderKey.value += 1
  loadCaptcha()
}

async function onLogin() {
  if (!form.username || !form.password) {
    ElMessage.warning(t('login.msg.need_credentials'))
    return
  }
  if (captcha.value.enabled && !form.sliderTicket && !form.captcha) {
    ElMessage.warning(t('login.msg.need_captcha'))
    return
  }
  loading.value = true
  recoveryHint.value = ''
  try {
    await auth.login(form.username, form.password, {
      captcha_id: captcha.value.id, captcha_code: form.captcha,
      slider_ticket: form.sliderTicket,
      otp_code: form.otp,
    })
    // 首次登录（默认密码）强制先改密
    if (auth.user?.must_change_password) {
      ElMessage.warning(t('login.need_change_password'))
      router.push('/change-password')
    } else {
      router.push('/')
    }
  } catch (e) {
    // 明确提示失败原因（用户不存在/密码错误/验证码/动态码）
    resetCaptcha()   // 验证票据一次性：失败后换新挑战
    const msg = e?.response?.data?.detail || e?.message || t('login.msg.failed')
    if (String(msg).includes('动态验证码')) {
      otpEnabledHint.value = true
      ElMessage.warning(t('login.msg.need_otp'))
    } else {
      ElMessage.error(String(msg))
    }
    // 找回兜底提示：文本由后端按来源/失败次数判定下发，外部来源恒为空
    try {
      const r = await http.get('/auth/recovery-hint', { params: { username: form.username } })
      recoveryHint.value = r?.hint || ''
    } catch { recoveryHint.value = '' }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-bg">
    <el-card class="login-card">
      <img src="/logo.png" :alt="t('login.brand')" class="login-logo" />
      <div class="login-title">{{ t('login.title') }}</div>
      <div class="login-sub">{{ t('login.subtitle') }}</div>
      <el-form @submit.prevent="onLogin">
        <el-form-item>
          <el-input v-model="form.username" :placeholder="t('login.form.username')" size="large" autofocus
                    autocomplete="username" name="username">
            <template #prefix><el-icon><i class="el-icon-user" /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.password"
            type="password"
            :placeholder="t('login.form.password')"
            size="large"
            show-password
            autocomplete="current-password"
            name="password"
            @keyup.enter="onLogin"
          >
            <template #prefix><el-icon><i class="el-icon-lock" /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-form-item v-if="captcha.enabled && captchaMode === 'slider'">
          <div class="captcha-slider">
            <SlideCaptcha :key="sliderKey" @success="onSliderOk" />
            <el-button link type="primary" size="small" class="captcha-switch" @click="switchCaptchaMode">
              {{ t('login.captcha.switch_to_text') }}
            </el-button>
          </div>
        </el-form-item>
        <el-form-item v-if="captcha.enabled && captchaMode === 'svg'">
          <div class="captcha-row">
            <el-input v-model="form.captcha" :placeholder="t('login.captcha.placeholder')" maxlength="4"
                      autocomplete="off" />
            <div class="captcha-svg" v-html="captcha.svg" @click="loadCaptcha" :title="t('login.captcha.refresh_title')" />
          </div>
          <el-button link type="primary" size="small" class="captcha-switch" @click="switchCaptchaMode">
            {{ t('login.captcha.switch_to_slider') }}
          </el-button>
        </el-form-item>
        <el-form-item v-if="otpEnabledHint || form.otp">
          <el-input v-model="form.otp" :placeholder="t('login.form.otp')" maxlength="6"
                    autocomplete="one-time-code" name="otp" />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="loading" @click="onLogin">
          {{ t('login.submit') }}
        </el-button>
      </el-form>
      <!-- 找回兜底提示：文案整段来自后端（含命令），前端不做静态下发——外部来源永远拿不到 -->
      <div v-if="recoveryHint" class="recovery-hint">{{ recoveryHint }}</div>
    </el-card>
    <div class="theme-corner">
      <el-switch
        :model-value="isDark"
        :active-text="t('login.theme.dark')"
        :inactive-text="t('login.theme.light')"
        @change="toggleTheme()"
      />
      <el-button link size="small" @click="setTheme('auto')">
        {{ themeMode === 'auto' ? t('login.theme.auto_on') : t('login.theme.auto') }}
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.captcha-row { display: flex; gap: 10px; width: 100%; align-items: center; }
.captcha-svg { cursor: pointer; border-radius: 8px; overflow: hidden; flex-shrink: 0; }
.captcha-svg :deep(svg) { display: block; }
.captcha-slider { width: 100%; display: flex; flex-direction: column; align-items: center; gap: 2px; }
.captcha-switch { align-self: flex-end; padding: 0; }
.recovery-hint {
  margin-top: 12px;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--el-color-warning);
  background: var(--el-color-warning-light-9);
  border-radius: 6px;
}
.login-bg {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-bg-color-page);
}
.login-card {
  width: 360px;
}
.login-logo {
  display: block;
  width: 72px;
  height: 72px;
  margin: 8px auto 12px;
  border-radius: 16px;
}
.login-title {
  font-size: 22px;
  font-weight: 600;
  text-align: center;
  margin-bottom: 4px;
}
.login-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: center;
  margin-bottom: 24px;
}
.theme-corner {
  position: fixed;
  right: 16px;
  bottom: 12px;
  display: flex;
  gap: 8px;
  align-items: center;
}
</style>
