/** Skill DTOs — mirror alex/kernel/dto/skill.py. */

export type SkillStatus = 'CANDIDATE' | 'ACTIVE' | 'DEPRECATED';

export interface SkillCard {
  id: string;
  name: string;
  description?: string;
  instruction?: string;
  pattern?: string;
  tags?: string[];
  status?: SkillStatus;
  useCount?: number;
  successCount?: number;
  failureCount?: number;
  createdAt?: string;
  updatedAt?: string;
}

export interface MergeSkillsResult {
  merged: number;
  deprecated: number;
  remaining: number;
  error?: string;
}