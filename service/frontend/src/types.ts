export interface GpuProcess {
  pid?: number;
  process_name?: string;
  used_memory_mib?: number;
}

export interface GpuDevice {
  index: number;
  uuid?: string;
  name?: string;
  memory_total_mib?: number;
  memory_free_mib?: number;
  utilization_gpu_percent?: number;
  temperature_c?: number | null;
  power_draw_w?: number | null;
  available?: boolean;
  availability_reason?: string;
  process_count?: number;
  processes?: GpuProcess[];
}

export interface Disk {
  path: string;
  total_bytes?: number;
  available_bytes?: number;
}

export interface NodeSoftware {
  os?: string;
  kernel?: string;
  architecture?: string;
  driver_version?: string;
  cuda_version?: string;
  tmux?: {
    installed?: boolean;
    version?: string;
    path?: string;
  };
  uv?: {
    installed?: boolean;
    version?: string;
    path?: string;
    python_list?: string | null;
    python_runtimes?: string[];
  };
  environments?: Array<{
    manager?: string;
    name?: string;
    path?: string;
    version?: string;
    source?: string;
    status?: string;
  }>;
  projects?: Array<{
    manager?: string;
    path?: string;
    lockfile?: string;
    file?: string;
  }>;
}

export interface EnvironmentRecord {
  alias: string;
  manager: string;
  path: string;
  version: string;
  project_path: string;
  python_version: string;
  status: string;
  source: string;
  reporter: string;
  evidence: string;
  reported_at: string;
  verified_at: string | null;
  conflict_detail?: Record<string, unknown>;
}

export interface NodeConnection {
  channel: string;
  alias: string;
  address: string;
  port: number;
  reachable: boolean;
}

export interface NodeCandidate {
  alias: string;
  channel: string;
  address: string;
  port: number;
  reachable: boolean;
  error?: string | null;
}

export interface NodeDocument {
  alias: string;
  mac?: string | null;
  hostname?: string | null;
  channel?: string;
  connection?: NodeConnection;
  candidates?: NodeCandidate[];
  work_path?: string | null;
  work_path_registered_at?: string | null;
  data_path?: string | null;
  data_path_registered_at?: string | null;
  model_path?: string | null;
  model_path_registered_at?: string | null;
  reachable?: boolean;
  stale?: boolean;
  errors?: string[];
  observed_at?: string | null;
  last_successful_at?: string | null;
  hardware?: {
    gpu?: { count?: number; available_count?: number; models?: string[]; devices?: GpuDevice[] };
    cpu?: { model?: string; logical_cores?: number; load_1m?: number };
    memory?: { total_bytes?: number; available_bytes?: number };
    disks?: Disk[];
  };
  software?: NodeSoftware;
  configured?: Record<string, unknown>;
  environment_record?: EnvironmentRecord | null;
  environment_status?: string | null;
}

export interface Snapshot {
  snapshot_id: number;
  generated_at: string;
  nodes: NodeDocument[];
  environment_records: EnvironmentRecord[];
  allocations: Allocation[];
  policy?: Record<string, unknown>;
  collector?: Record<string, unknown>;
}

export interface Allocation {
  alias: string;
  gpu_index: number;
  target: string;
  assigned_by: string;
  assigned_at: string;
  expires_at: string | null;
  notes: string;
}

export interface PlanDevice {
  alias: string;
  gpu_index: number;
  gpu_name?: string | null;
}

export interface PlanRecord {
  plan_id: string;
  name: string;
  owner: string;
  scope: string;
  status: string;
  claimed_by: string;
  claimed_at: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
  devices: PlanDevice[];
}

export interface DirectoryEntry {
  name: string;
  size_bytes: number;
}

export interface DirectoryList {
  alias: string;
  kind: "data" | "model";
  path: string;
  entries: DirectoryEntry[];
}

export interface UvCachedPackage {
  name: string;
  version: string;
}

export interface UvLock {
  path: string;
  project: string;
  packages: UvCachedPackage[];
  error?: string;
}

export interface UvCacheInfo {
  alias: string;
  uv_path: string;
  cache_dir: string;
  cache_size_bytes: number;
  python_versions: string[];
  packages: UvCachedPackage[];
  installed: UvCachedPackage[];
  locks: UvLock[];
}

export interface SeriesSample {
  generated_at: string;
  gpu_index: number;
  uuid?: string;
  name?: string;
  memory_total_mib?: number;
  memory_free_mib?: number;
  utilization_percent?: number;
}

export interface NvitopProcess {
  pid: number;
  name: string;
  user: string;
  memory_mib: number;
  command: string;
}

export interface NvitopDevice {
  index: number;
  name: string;
  memory_total_mib: number;
  memory_used_mib: number;
  utilization_percent: number;
  temperature: number | null;
  power_w: number | null;
  processes: NvitopProcess[];
}

export interface NvitopPayload {
  alias: string;
  observed_at: string;
  cpu_percent: number;
  memory_total_mib: number;
  memory_used_mib: number;
  devices: NvitopDevice[];
  samples?: SeriesSample[];
}
