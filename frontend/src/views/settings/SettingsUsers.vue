<script setup>
// 用户管理（仅管理员）：账号 / 密码 / 角色三档。
// 刻意不提供部门、组织架构、Namespace 授权——那些是访问层的另一条线。
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  createUser, deleteUser, listUsers, resetUserPassword, updateUser,
} from '@/api/settings'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const auth = useAuthStore()
const currentUserId = computed(() => auth.user?.id)

const ROLES = ['admin', 'operator', 'viewer']
const ROLE_TAG = { admin: 'danger', operator: 'warning', viewer: 'info' }
const roleLabel = (r) => t(`settings.role.${r}`)

const users = ref([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    users.value = await listUsers()
  } catch { /* 拦截器 */ } finally {
    loading.value = false
  }
}

/* ---------------- 新建 / 编辑 ---------------- */
const dialog = ref(false)
const saving = ref(false)
const editing = ref(null)   // null=新建
const form = reactive({
  username: '',
  display_name: '',
  role: 'viewer',
  password: '',
  password_mode: 'generated',   // generated=服务端生成一次性密码；manual=管理员手填
  must_change_password: true,
  is_active: true,
})

function resetForm() {
  Object.assign(form, {
    username: '',
    display_name: '',
    role: 'viewer',
    password: '',
    password_mode: 'generated',
    must_change_password: true,
    is_active: true,
  })
}

function openCreate() {
  editing.value = null
  resetForm()
  dialog.value = true
}

function openEdit(row) {
  editing.value = row
  Object.assign(form, {
    username: row.username,
    display_name: row.display_name || '',
    role: row.role,
    password: '',
    password_mode: 'generated',
    must_change_password: row.must_change_password,
    is_active: row.is_active,
  })
  dialog.value = true
}

async function submit() {
  if (!editing.value) {
    if (!form.username.trim()) return ElMessage.warning(t('settings.user.msg.need_username'))
    if (form.password_mode === 'manual' && !form.password) {
      return ElMessage.warning(t('settings.user.msg.need_password'))
    }
  }
  saving.value = true
  try {
    if (editing.value) {
      // 改密走「重置密码」独立入口，这里只改资料/角色/启停
      const payload = {
        display_name: form.display_name,
        is_active: form.is_active,
      }
      // 防呆：不允许把自己降级（后端也会拦，前端直接不提交该字段）
      if (editing.value.id !== currentUserId.value) payload.role = form.role
      await updateUser(editing.value.id, payload)
      ElMessage.success(t('settings.user.msg.updated'))
    } else {
      const payload = {
        username: form.username.trim(),
        display_name: form.display_name.trim(),
        role: form.role,
        must_change_password: form.must_change_password,
      }
      if (form.password_mode === 'manual') payload.password = form.password
      const r = await createUser(payload)
      if (r.initial_password) {
        // 新建接口不回传 username，直接用提交值（已 trim），避免显示 undefined
        showSecret(payload.username, r.initial_password)
      } else {
        ElMessage.success(t('settings.user.msg.created'))
      }
    }
    dialog.value = false
    load()
  } catch { /* 拦截器 */ } finally {
    saving.value = false
  }
}

/* ---------------- 启用 / 禁用 ---------------- */
async function setActive(row, value) {
  try {
    await updateUser(row.id, { is_active: value })
    row.is_active = value
    ElMessage.success(t('settings.user.msg.updated'))
  } catch { /* 拦截器 */ }
}

/* ---------------- 重置密码 ---------------- */
async function doReset(row) {
  await ElMessageBox.confirm(
    t('settings.user.confirm.reset', { name: row.username }),
    t('settings.user.confirm.reset_title'),
    { type: 'warning', confirmButtonText: t('settings.user.btn.reset_pwd'), cancelButtonText: t('settings.btn.cancel') },
  )
  const r = await resetUserPassword(row.id)
  load()
  showSecret(row.username, r.temporary_password)
}

/* ---------------- 删除 ---------------- */
async function remove(row) {
  await ElMessageBox.confirm(
    t('settings.user.confirm.delete', { name: row.username }),
    t('settings.user.confirm.delete_title'),
    { type: 'warning', confirmButtonText: t('settings.user.btn.delete'), cancelButtonText: t('settings.btn.cancel') },
  )
  await deleteUser(row.id)
  ElMessage.success(t('settings.user.msg.deleted'))
  load()
}

/* ---------------- 一次性密码回显 ---------------- */
const secret = reactive({ open: false, username: '', password: '' })

function showSecret(username, password) {
  Object.assign(secret, { open: true, username, password })
}

function copySecret() {
  navigator.clipboard.writeText(secret.password)
  ElMessage.success(t('settings.msg.copied'))
}

onMounted(load)
</script>

<template>
  <div class="users-page">
    <div class="toolbar">
      <el-button type="primary" @click="openCreate">{{ t('settings.user.btn.new') }}</el-button>
      <span class="tip">{{ t('settings.user.tip') }}</span>
    </div>

    <el-table stripe :data="users" v-loading="loading" border>
      <el-table-column prop="username" :label="t('settings.user.col.username')" width="170">
        <template #default="{ row }">
          <span class="mono">{{ row.username }}</span>
          <el-tag v-if="row.id === currentUserId" size="small" class="self-tag">{{ t('settings.user.self') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="display_name" :label="t('settings.user.col.display_name')" width="160" show-overflow-tooltip />
      <el-table-column :label="t('settings.user.col.role')" width="150">
        <template #default="{ row }">
          <el-tag size="small" :type="ROLE_TAG[row.role]">{{ roleLabel(row.role) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('settings.user.col.status')" width="160">
        <template #default="{ row }">
          <el-tag v-if="!row.is_active" size="small" type="info">{{ t('settings.user.status.disabled') }}</el-tag>
          <el-tag v-else-if="row.must_change_password" size="small" type="warning">{{ t('settings.user.status.pending') }}</el-tag>
          <el-tag v-else size="small" type="success">{{ t('settings.user.status.active') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('settings.user.col.last_login')" width="180">
        <template #default="{ row }">{{ row.last_login_at || t('settings.user.never_login') }}</template>
      </el-table-column>
      <el-table-column :label="t('settings.user.col.enabled')" width="90">
        <template #default="{ row }">
          <el-switch
            :model-value="row.is_active"
            :disabled="row.id === currentUserId"
            @change="(v) => setActive(row, v)"
          />
        </template>
      </el-table-column>
      <el-table-column :label="t('settings.col.actions')" width="240" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">{{ t('settings.user.btn.edit') }}</el-button>
          <el-button size="small" :disabled="row.id === currentUserId" @click="doReset(row)">{{ t('settings.user.btn.reset_pwd') }}</el-button>
          <el-button
            size="small" type="danger"
            :disabled="row.id === currentUserId"
            @click="remove(row)"
          >{{ t('settings.user.btn.delete') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建 / 编辑 -->
    <el-dialog
      v-model="dialog"
      :title="editing ? t('settings.user.dialog.edit_title') : t('settings.user.dialog.create_title')"
      width="520px"
    >
      <el-form label-width="120px">
        <el-form-item :label="t('settings.user.form.username')" required>
          <el-input v-model="form.username" :placeholder="t('settings.user.ph.username')" maxlength="64" :disabled="!!editing" />
        </el-form-item>
        <el-form-item :label="t('settings.user.form.display_name')">
          <el-input v-model="form.display_name" :placeholder="t('settings.user.ph.display_name')" maxlength="64" />
        </el-form-item>
        <el-form-item :label="t('settings.user.form.role')" required>
          <el-select v-model="form.role" style="width: 100%" :disabled="!!editing && editing.id === currentUserId">
            <el-option v-for="r in ROLES" :key="r" :label="roleLabel(r)" :value="r" />
          </el-select>
        </el-form-item>

        <template v-if="!editing">
          <el-form-item :label="t('settings.user.form.password')">
            <el-radio-group v-model="form.password_mode">
              <el-radio value="generated">{{ t('settings.user.mode.generated') }}</el-radio>
              <el-radio value="manual">{{ t('settings.user.mode.manual') }}</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="form.password_mode === 'manual'" :label="t('settings.user.form.password')" required>
            <el-input v-model="form.password" type="password" show-password :placeholder="t('settings.user.ph.password')" maxlength="64" />
          </el-form-item>
          <el-form-item :label="t('settings.user.form.must_change')">
            <el-checkbox v-model="form.must_change_password">{{ t('settings.user.form.must_change_desc') }}</el-checkbox>
          </el-form-item>
        </template>

        <el-form-item v-if="editing" :label="t('settings.user.form.enabled')">
          <el-switch v-model="form.is_active" :disabled="editing.id === currentUserId" />
        </el-form-item>
      </el-form>
      <div v-if="editing" class="dlg-tip">{{ t('settings.user.reset_hint') }}</div>
      <div v-if="editing && editing.id === currentUserId" class="dlg-tip">{{ t('settings.user.self_hint') }}</div>
      <template #footer>
        <el-button @click="dialog = false">{{ t('settings.btn.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="submit">{{ t('settings.btn.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 一次性密码：只在本次弹窗可见，关闭后不可再取 -->
    <el-dialog v-model="secret.open" :title="t('settings.user.pwd.title')" width="520px">
      <div class="pwd-warn">{{ t('settings.user.pwd.warn') }}</div>
      <el-form label-width="90px" class="pwd-form">
        <el-form-item :label="t('settings.user.col.username')">
          <span class="mono">{{ secret.username }}</span>
        </el-form-item>
        <el-form-item :label="t('settings.user.form.password')">
          <span class="mono pwd-plain">{{ secret.password }}</span>
        </el-form-item>
      </el-form>
      <div class="dlg-tip">{{ t('settings.user.pwd.tip') }}</div>
      <template #footer>
        <el-button type="primary" @click="copySecret">{{ t('settings.btn.copy') }}</el-button>
        <el-button @click="secret.open = false">{{ t('settings.btn.close') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.users-page { display: flex; flex-direction: column; gap: 4px; }
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.self-tag { margin-left: 6px; }
.dlg-tip { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.pwd-warn { color: var(--el-color-warning); font-size: 13px; margin-bottom: 12px; }
.pwd-plain {
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  user-select: all;
}
</style>
