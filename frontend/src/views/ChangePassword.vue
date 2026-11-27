<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import http from '@/api/http'
import Watermark from '@/components/Watermark.vue'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

// OTP（TOTP 二步）能力保留在后端，当前 OTP_ENABLED=false，本页不展示入口。

onMounted(() => {
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
  </div>
</template>

<style scoped>
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
