<script setup>
// 云凭证只存在于当前表单内存：不写浏览器存储，不进入聊天或任务产物。
import { computed, onDeactivated, ref } from 'vue'
import { t } from '@/i18n'

const props = defineProps({
  name: { type: String, default: '' },
  ak: { type: String, default: '' },
  sk: { type: String, default: '' },
  region: { type: String, default: '' },
  vendor: { type: String, default: 'aliyun' },
  editing: { type: Boolean, default: false },
  showIdentity: { type: Boolean, default: true },
  idPrefix: { type: String, default: 'cloud-credential' },
})
const emit = defineEmits(['update:name', 'update:ak', 'update:sk', 'update:region', 'change'])

function update(key, value) {
  emit(`update:${key}`, value)
  emit('change', { key })
}
function clearSecrets() {
  if (props.ak) emit('update:ak', '')
  if (props.sk) emit('update:sk', '')
}

const OFFICIAL_CONSOLES = {
  aliyun: 'https://ram.console.aliyun.com/permissions',
  tencent: 'https://console.cloud.tencent.com/cam/policy',
  volc: 'https://console.volcengine.com/iam/identitymanage/policy',
  baidu: 'https://console.bce.baidu.com/iam/',
  huawei: 'https://console.huaweicloud.com/iam/',
  aws: 'https://console.aws.amazon.com/iam/',
  azure: 'https://portal.azure.com/#browse/Microsoft.Authorization%2FroleAssignments',
}
const permissionKeys = ['assets', 'metrics', 'logs', 'cost', 'changes']
const officialConsole = computed(() => OFFICIAL_CONSOLES[props.vendor] || OFFICIAL_CONSOLES.aliyun)
const requestText = computed(() => [
  t('clouds.credential.request_intro', { vendor: t(`clouds.vendor.${props.vendor}`) }),
  ...permissionKeys.map((key, index) => `${index + 1}. ${t(`clouds.credential.permission.${key}`)}`),
  t('clouds.credential.request_footer'),
].join('\n'))
const copyState = ref('')
async function copyRequest() {
  try {
    await navigator.clipboard.writeText(requestText.value)
    copyState.value = 'copied'
  } catch {
    copyState.value = 'copy_failed'
  }
}

// 条件步进会卸载组件，但此时凭证尚未提交，不能提前清空；
// 页面、对话框和 KeepAlive 退出由父级及此钩子负责清空。
onDeactivated(clearSecrets)
</script>

<template>
  <div class="credential-fields" :data-credential-form="idPrefix">
    <el-form-item v-if="showIdentity" :label="t('clouds.form.account_name')">
      <el-input
        :model-value="name" :id="`${idPrefix}-name`" maxlength="64" autocomplete="off"
        :placeholder="t('clouds.form.name_ph_cloud')" @update:model-value="update('name', $event)"
      />
    </el-form-item>
    <el-form-item label="AccessKey ID">
      <el-input
        :model-value="ak" :id="`${idPrefix}-ak`" type="password" show-password maxlength="256"
        :placeholder="editing ? t('clouds.form.keep_blank') : t('clouds.form.ak_ph')"
        autocomplete="new-password" autocapitalize="none" spellcheck="false"
        @update:model-value="update('ak', $event)"
      />
    </el-form-item>
    <el-form-item label="AccessKey Secret">
      <el-input
        :model-value="sk" :id="`${idPrefix}-sk`" type="password" show-password maxlength="256"
        :placeholder="editing ? t('clouds.form.keep_blank') : t('clouds.form.sk_ph')"
        autocomplete="new-password" autocapitalize="none" spellcheck="false"
        @update:model-value="update('sk', $event)"
      />
    </el-form-item>
    <el-form-item v-if="showIdentity" :label="t('clouds.form.region')">
      <el-input
        :model-value="region" :id="`${idPrefix}-region`" maxlength="64" autocomplete="off"
        :placeholder="t('clouds.form.region_ph')" @update:model-value="update('region', $event)"
      />
    </el-form-item>
    <el-collapse v-if="showIdentity" class="credential-authorization">
      <el-collapse-item :title="t('clouds.credential.no_credentials')">
        <div class="credential-request-summary">{{ t('clouds.credential.request_summary') }}</div>
        <ul class="credential-permissions">
          <li v-for="key in permissionKeys" :key="key">{{ t(`clouds.credential.permission.${key}`) }}</li>
        </ul>
        <a :href="officialConsole" target="_blank" rel="noopener noreferrer" class="credential-official">
          {{ t('clouds.credential.official_console') }}
        </a>
        <el-input :model-value="requestText" type="textarea" :rows="5" readonly class="credential-request-text" />
        <el-button size="small" @click="copyRequest">{{ t('clouds.credential.copy_request') }}</el-button>
        <span v-if="copyState" class="credential-copy-state">{{ t(`clouds.credential.${copyState}`) }}</span>
      </el-collapse-item>
    </el-collapse>
    <div class="credential-note">{{ t('clouds.credential.memory_only') }}</div>
  </div>
</template>

<style scoped>
.credential-note { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; margin: 4px 0; }
.credential-authorization { margin: 0 0 8px 110px; }
.credential-request-summary { font-size: 13px; line-height: 1.7; }
.credential-permissions { margin: 8px 0; padding-left: 20px; color: var(--el-text-color-regular); font-size: 12px; line-height: 1.7; }
.credential-official { display: inline-block; margin-bottom: 8px; color: var(--el-color-primary); font-size: 13px; }
.credential-request-text { display: block; margin-bottom: 8px; }
.credential-copy-state { margin-left: 8px; color: var(--el-text-color-secondary); font-size: 12px; }
@media (max-width: 640px) { .credential-authorization { margin-left: 0; } }
</style>
