/** 用户头像：有自定义头像用自定义图，否则用内置默认头像。
 *  右上角、设置页、个人资料共用，保证尺寸/描边一致。 */
export default function Avatar({
  src,
  name,
  size = 28,
  className = "",
}: {
  src?: string | null;
  name?: string | null;
  size?: number;
  className?: string;
}) {
  const url = src || "/default-avatar.png";
  return (
    <img
      src={url}
      alt={name ? `${name} 的头像` : "用户头像"}
      width={size}
      height={size}
      className={`ga-avatar rounded-full object-cover shrink-0 ${className}`}
      style={{ width: size, height: size }}
      onError={(e) => {
        // 头像文件被删/换服务时不留破图：退回默认头像
        const img = e.currentTarget;
        if (img.dataset.fallback) return;
        img.dataset.fallback = "1";
        img.src = "/default-avatar.png";
      }}
    />
  );
}
