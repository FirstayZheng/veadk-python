import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function BaseIcon({ children, ...props }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export function DeployTaskListIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M7.25 6.25h12" />
      <path d="M7.25 12h12" />
      <path d="M7.25 17.75h12" />
      <path d="M4.25 6.25h.01" />
      <path d="M4.25 12h.01" />
      <path d="M4.25 17.75h.01" />
    </BaseIcon>
  );
}

export function DeployTaskRunningIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M12 4.25a7.75 7.75 0 1 0 7.08 4.6" />
      <path d="M12 7.1v5.05l3.2 1.9" />
    </BaseIcon>
  );
}

export function DeployTaskSuccessIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <circle cx="12" cy="12" r="8" />
      <path d="m8.4 12.25 2.25 2.25 4.95-5" />
    </BaseIcon>
  );
}

export function DeployTaskErrorIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M12 4.25 21 19.75H3L12 4.25Z" />
      <path d="M12 9.25v4.15" />
      <path d="M12 16.75h.01" />
    </BaseIcon>
  );
}

export function DeployTaskCancelledIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <circle cx="12" cy="12" r="8" />
      <path d="m9.2 9.2 5.6 5.6" />
      <path d="m14.8 9.2-5.6 5.6" />
    </BaseIcon>
  );
}

export function DeployTaskChevronIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="m7.25 9.5 4.75 4.75 4.75-4.75" />
    </BaseIcon>
  );
}

export function DeployTaskChatIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M5.75 5.25h12.5a2 2 0 0 1 2 2v7.25a2 2 0 0 1-2 2H11.3l-4.05 3v-3.05h-1.5a2 2 0 0 1-2-2v-7.2a2 2 0 0 1 2-2Z" />
      <path d="M8.25 9.25h7.5" />
      <path d="M8.25 12.25h4.9" />
    </BaseIcon>
  );
}

export function DeployTaskCopyIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <rect x="8.25" y="7.25" width="10" height="12" rx="1.75" />
      <path d="M5.75 15.75V5.85c0-.88.72-1.6 1.6-1.6h7.9" />
    </BaseIcon>
  );
}

export function DeployTaskCopiedIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="m5.75 12.35 3.65 3.65 8.85-8.85" />
    </BaseIcon>
  );
}

export function DeployTaskConsoleIcon(props: IconProps) {
  return (
    <BaseIcon {...props}>
      <path d="M6.5 5.25h7.75a2 2 0 0 1 2 2v1" />
      <path d="M11.5 18.75h-5a2 2 0 0 1-2-2V7.25a2 2 0 0 1 2-2" />
      <path d="M13.25 10.75h6v6" />
      <path d="m19.25 10.75-8.5 8.5" />
    </BaseIcon>
  );
}
