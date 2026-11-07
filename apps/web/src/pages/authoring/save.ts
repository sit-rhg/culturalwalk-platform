/**
 * 保存草稿节点（PUT /route-drafts/{id}/nodes）。
 *
 * 后端用 If-Match 做乐观锁，失败分两类：
 * - 412 STALE_DRAFT_VERSION：别处已经把草稿存过一版，本地快照作废，必须重新拉一份再改；
 * - 其它（422 校验、409 幂等键复用…）：原样交给页面按错误信封提示。
 * 单独拎出来是为了能在没有 DOM 的环境里测到「412 会自动重拉最新草稿」这条分支。
 */
import { isStaleDraftVersion } from '@/api/adapters'
import type { RouteDraftDetail } from '@/api/adapters'
import { fetchDraftDetail, replaceDraftNodes } from '@/api/endpoints'
import type { Schemas } from '@/api/types'

export const STALE_DRAFT_NOTICE = '这份草稿刚在别处被保存过，已载入最新版本，请确认节点后再保存'
export const STALE_DRAFT_RELOAD_FAILED =
  '这份草稿刚在别处被保存过，自动载入最新版本失败，请刷新页面后再保存'

export type SaveNodesResult =
  | { ok: true; draft: RouteDraftDetail }
  | { ok: false; stale: true; draft: RouteDraftDetail | null; message: string }
  | { ok: false; stale: false; error: unknown }

export async function saveDraftNodes(
  draftId: string,
  nodes: Schemas['RouteNodeInput'][],
  lockVersion: number,
): Promise<SaveNodesResult> {
  try {
    return { ok: true, draft: await replaceDraftNodes(draftId, nodes, lockVersion) }
  } catch (error) {
    if (!isStaleDraftVersion(error)) return { ok: false, stale: false, error }

    // 重拉失败也要给出可读提示，不能让用户对着作废的快照以为还能存
    let fresh: RouteDraftDetail | null = null
    try {
      fresh = await fetchDraftDetail(draftId)
    } catch {
      fresh = null
    }
    return {
      ok: false,
      stale: true,
      draft: fresh,
      message: fresh ? STALE_DRAFT_NOTICE : STALE_DRAFT_RELOAD_FAILED,
    }
  }
}
