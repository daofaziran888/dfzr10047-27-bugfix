/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  可执行动作?: string[]
  资格说明?: string
  [field: string]: string | number | boolean | string[] | Receipt | null | undefined
}

export type Receipt = {
  回执编号?: string
  差异编号?: string
  版本对?: string
  结论?: string
  原因?: string
  命中字段?: string
  验证批次?: string[]
  起始位点?: string
  截止位点?: string
  确认时间?: string
  复用自?: string
  处理批次?: string[]
  处理结论?: string
  已发布?: boolean
}

export type EligibilityApplication = {
  差异编号: string
  旧模板版本?: string
  新模板版本?: string
  登记时间?: string
  可执行: boolean
  拒绝原因?: string
  零变化结论?: string
  范围校验回执?: Receipt | null
  重解析任务编号?: string
  任务状态?: string
}

export type EligibilityInfo = EligibilityApplication & {
  比较结论?: string
  差异字段?: string[]
  比较详情可打开?: boolean
  产物可核对?: boolean
}

export type EntryDetail = {
  entry: EntryRow
  extra: {
    资格结论?: EligibilityInfo
    已验证范围?: { 起始位点?: string; 截止位点?: string; 受影响批次?: string }
    范围校验回执?: Receipt | null
    执行回执?: Receipt | null
    处理结果?: string
    可执行?: boolean
    拒绝原因?: string
    同批申请?: EligibilityApplication[]
  }
}

export type BatchEligibility = {
  batch: string
  applications: EligibilityApplication[]
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
