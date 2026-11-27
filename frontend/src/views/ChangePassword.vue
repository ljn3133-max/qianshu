<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import http from '@/api/http'
import Watermark from '@/components/Watermark.vue'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

// ---- 动态验证码（TOTP 二步，登录安全矩阵）----
const otp = reactive({ enabled: false, secret: '', uri: '', code: '' })

async function loadOtpState() {
  try {
    const me = await http.get('/auth/me')
    otp.enabled = !!me.otp_enabled
  } catch { /* 拦截器 */ }
}

async function otpSetup() {
  try {
    const r = await http.post('/auth/otp/setup')
    otp.secret = r.secret
    otp.uri = r.otpauth_uri
    ElMessage.success(t('login.otp.msg.setup'))
  } catch { /* 拦截器 */ }
}

async function otpConfirm() {
  try {
    await http.post('/auth/otp/confirm', { code: otp.code })
    otp.enabled = true
    otp.secret = ''
    ElMessage.success(t('login.otp.msg.enabled'))
  } catch { /* 拦截器 */ }
}

async function otpDisable() {
  try {
    await http.post('/auth/otp/disable', { code: otp.code })
    otp.enabled = false
    otp.secret = ''
    ElMessage.success(t('login.otp.msg.disabled'))
  } catch { /* 拦截器 */ }
}

onMounted(() => {
  loadOtpState()
  // user 未加载时 forced 不能凭空为真：进页补拉 me，避免误报"初始密码必须修改"
  if (!auth.user) auth.fetchMe().catch(() => {})
})

const router = useRouter()
const auth = useAuthStore()

// 仅显式 true 才算强制改密（undefined/null 不误报）
const forced = computed(() => auth.user?.must_change_password === true)
const form = reactive({ old_password: '', new_password: '', confirm: '' })
const loading = ref(false)

async function onSubmit() {
  if (!form.old_password || !form.new_password) {
    ElMessage.warning(t('login.pwd.msg.need_both'))
    return
  }
  if (form.new_password.length < 8) {
    ElMessage.warning(t('login.pwd.msg.too_short'))
    return
  }
  if (form.new_password !== form.confirm) {
    ElMessage.warning(t('login.pwd.msg.mismatch'))
    return
  }
  loading.value = true
  try {
    await http.post('/auth/password', {
      old_password: form.old_password,
      new_password: form.new_password,
    })
    if (auth.user) auth.user.must_change_password = false
    ElMessage.success(forced.value ? t('login.pwd.msg.changed_forced') : t('login.pwd.msg.changed'))
    router.push('/')
  } catch {
    /* 错误提示由 http 拦截器统一处理 */
  } finally {
    loading.value = false
  }
}

function onLogout() {
  auth.logout()
  router.push('/login')
}
</script>

<template>
  <Watermark />
  <div class="pwd-bg">
    <el-card class="pwd-card">
      <div class="pwd-title">{{ t('login.pwd.title') }}</div>
      <div class="pwd-sub">
        {{ forced ? t('login.pwd.forced_sub') : t('login.pwd.sub') }}
      </div>
      <el-alert
        v-if="forced"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
        :title="t('login.pwd.forced_alert')"
      />
      <el-form @submit.prevent="onSubmit">
        <el-form-item>
          <el-input
            v-model="form.old_password"
            type="password"
            :placeholder="t('login.pwd.old_ph')"
            size="large"
            show-password
            autofocus
          >
            <template #prefix><el-icon><i class="el-icon-lock" /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.new_password"
            type="password"
            :placeholder="t('login.pwd.new_ph')"
            size="large"
            show-password
          >
            <template #prefix><el-icon><i class="el-icon-key" /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.confirm"
            type="password"
            :placeholder="t('login.pwd.confirm_ph')"
            size="large"
            show-password
            @keyup.enter="onSubmit"
          >
            <template #prefix><el-icon><i class="el-icon-key" /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="loading" @click="onSubmit">
          {{ t('login.pwd.submit') }}
        </el-button>
        <el-button v-if="forced" link size="small" style="width: 100%; margin: 12px 0 0" @click="onLogout">
          {{ t('login.pwd.switch_account') }}
        </el-button>
      </el-form>
    </el-card>

    <el-card class="pwd-card" style="margin-top: 16px">
      <template #header><b>{{ t('login.otp.title') }}</b></template>
      <div class="tip">
        {{ t('login.otp.desc') }}
      </div>
      <div v-if="otp.enabled" class="otp-row">
        <el-tag type="success">{{ t('login.otp.enabled') }}</el-tag>
        <el-input v-model="otp.code" :placeholder="t('login.otp.disable_ph')" maxlength="6" style="width: 220px" />
        <el-button type="danger" plain @click="otpDisable">{{ t('login.otp.disable_btn') }}</el-button>
      </div>
      <template v-else>
        <div v-if="otp.secret" class="otp-row">
          <div>
            <div class="tip">{{ t('login.otp.secret_label') }}</div>
            <code class="mono">{{ otp.secret }}</code>
            <div class="tip" style="margin-top: 4px">{{ t('login.otp.uri', { uri: otp.uri.slice(0, 60) }) }}</div>
          </div>
          <el-input v-model="otp.code" :placeholder="t('login.otp.code_ph')" maxlength="6" style="width: 220px" />
          <el-button type="primary" @click="otpConfirm">{{ t('login.otp.activate') }}</el-button>
        </div>
        <el-button v-else type="primary" plain @click="otpSetup">{{ t('login.otp.setup_btn') }}</el-button>
      </template>
    </el-card>
  </div>
</template>

<style scoped>
.otp-row { display: flex; gap: 12px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
.tip { font-size: 12.5px; color: var(--el-text-color-secondary); margin-top: 6px; }
.mono { font-family: Consolas, monospace; }
.pwd-bg {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-bg-color-page);
}
.pwd-card {
  width: 360px;
}
.pwd-title {
  font-size: 22px;
  font-weight: 600;
  text-align: center;
  margin-bottom: 4px;
}
.pwd-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: center;
  margin-bottom: 16px;
}
</style>
