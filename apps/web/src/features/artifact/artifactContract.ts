import type {
  ArtifactDTO,
  ArtifactProducerDTO,
  ArtifactPurpose,
} from "@jarvis/shared";

type ArtifactV1Compatible = Omit<ArtifactDTO, "purpose" | "producer"> & {
  purpose?: ArtifactPurpose;
  producer?: ArtifactProducerDTO;
};

/**
 * 将历史 artifact.created v1 事件升级为前端统一消费的 v2 视图。
 *
 * PostgreSQL Artifact DTO 已始终返回显式 v2 字段；该兼容仅服务不可变的历史
 * RuntimeEvent，避免刷新旧任务时把最终回复重新误判为交付物。
 */
export function normalizeArtifact(
  artifact: ArtifactDTO | ArtifactV1Compatible,
): ArtifactDTO {
  const purpose =
    artifact.purpose ??
    (artifact.metadata?.is_final_output === true
      ? "final_response"
      : "deliverable");
  const producer =
    artifact.producer?.type === "tool" && artifact.producer.tool_call_id
      ? artifact.producer
      : { type: "runtime" as const };
  return { ...artifact, purpose, producer };
}
