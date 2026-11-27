// 表格列宽拖拽：拖动表头右缘调整列宽。
// 拖表头右缘 6px 调列宽；宽值按「页面+列序」存 localStorage，刷新后记忆。
import { onBeforeUnmount } from 'vue'

function colGroups(el) {
  return el.querySelectorAll('colgroup')
}

function applyWidth(el, idx, w) {
  colGroups(el).forEach((cg) => {
    const col = cg.children[idx]
    if (col) col.style.width = w + 'px'
  })
}

function init(el) {
  const onDown = (e) => {
    const th = e.target.closest && e.target.closest('th.el-table__cell')
    if (!th) return
    const rect = th.getBoundingClientRect()
    if (e.clientX < rect.right - 7 || e.clientX > rect.right + 2) return
    const idx = Array.from(th.parentElement.children).indexOf(th)
    const startX = e.clientX
    const startW = rect.width
    const key = `qianshu.colw.${location.pathname}.${idx}`
    const onMove = (ev) => {
      const w = Math.max(56, Math.round(startW + ev.clientX - startX))
      applyWidth(el, idx, w)
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      const th2 = th.getBoundingClientRect()
      try { localStorage.setItem(key, String(Math.round(th2.width))) } catch { /* 满了也不碍事 */ }
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    e.preventDefault()
    e.stopPropagation()
  }
  el.addEventListener('mousedown', onDown)
  // 恢复记忆
  requestAnimationFrame(() => {
    Array.from(el.querySelectorAll('th.el-table__cell')).forEach((th, idx) => {
      const key = `qianshu.colw.${location.pathname}.${idx}`
      const saved = Number(localStorage.getItem(key) || 0)
      if (saved > 0) applyWidth(el, idx, saved)
    })
  })
  el.__colwDispose = () => el.removeEventListener('mousedown', onDown)
}

export const TableResize = {
  mounted(el) { init(el) },
  unmounted(el) { el.__colwDispose && el.__colwDispose() },
}

export default {
  install(app) {
    app.directive('table-resize', TableResize)
  },
}
