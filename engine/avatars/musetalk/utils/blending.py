from PIL import Image
import numpy as np
import cv2
import copy


def get_crop_box(box, expand):
    x, y, x1, y1 = [int(v) for v in box]  # 1003fix: 卡通float box守卫
    x_c, y_c = (x+x1)//2, (y+y1)//2
    w, h = x1-x, y1-y
    s = int(max(w, h)//2*expand)
    crop_box = [x_c-s, y_c-s, x_c+s, y_c+s]
    return crop_box, s


def face_seg(image, mode="raw", fp=None):
    """
    对图像进行面部解析，生成面部区域的掩码。

    Args:
        image (PIL.Image): 输入图像。

    Returns:
        PIL.Image: 面部区域的掩码图像。
    """
    seg_image = fp(image, mode=mode)  # 使用 FaceParsing 模型解析面部
    if seg_image is None:
        print("error, no person_segment")  # 如果没有检测到面部，返回错误
        return None

    seg_image = seg_image.resize(image.size)  # 将掩码图像调整为输入图像的大小
    return seg_image


def get_image(image, face, face_box, upper_boundary_ratio=0.5, expand=1.5, mode="raw", fp=None):
    """
    将裁剪的面部图像粘贴回原始图像，并进行一些处理。

    Args:
        image (numpy.ndarray): 原始图像（身体部分）。
        face (numpy.ndarray): 裁剪的面部图像。
        face_box (tuple): 面部边界框的坐标 (x, y, x1, y1)。
        upper_boundary_ratio (float): 用于控制面部区域的保留比例。
        expand (float): 扩展因子，用于放大裁剪框。
        mode: 融合mask构建方式 

    Returns:
        numpy.ndarray: 处理后的图像。
    """
    # 将 numpy 数组转换为 PIL 图像
    body = Image.fromarray(image[:, :, ::-1])  # 身体部分图像(整张图)
    face = Image.fromarray(face[:, :, ::-1])  # 面部图像

    x, y, x1, y1 = face_box  # 获取面部边界框的坐标
    crop_box, s = get_crop_box(face_box, expand)  # 计算扩展后的裁剪框
    x_s, y_s, x_e, y_e = crop_box  # 裁剪框的坐标
    face_position = (x, y)  # 面部在原始图像中的位置

    # 从身体图像中裁剪出扩展后的面部区域（下巴到边界有距离）
    face_large = body.crop(crop_box)
        
    ori_shape = face_large.size  # 裁剪后图像的原始尺寸

    # 对裁剪后的面部区域进行面部解析，生成掩码
    mask_image = face_seg(face_large, mode=mode, fp=fp)
    
    mask_small = mask_image.crop((x - x_s, y - y_s, x1 - x_s, y1 - y_s))  # 裁剪出面部区域的掩码
    
    mask_image = Image.new('L', ori_shape, 0)  # 创建一个全黑的掩码图像
    mask_image.paste(mask_small, (x - x_s, y - y_s, x1 - x_s, y1 - y_s))  # 将面部掩码粘贴到全黑图像上
    
    
    # 保留面部区域的上半部分（用于控制说话区域）
    width, height = mask_image.size
    top_boundary = int(height * upper_boundary_ratio)  # 计算上半部分的边界
    modified_mask_image = Image.new('L', ori_shape, 0)  # 创建一个新的全黑掩码图像
    modified_mask_image.paste(mask_image.crop((0, top_boundary, width, height)), (0, top_boundary))  # 粘贴上半部分掩码
    
    
    # 对掩码进行高斯模糊，使边缘更平滑
    blur_kernel_size = int(0.05 * ori_shape[0] // 2 * 2) + 1  # 计算模糊核大小
    mask_array = cv2.GaussianBlur(np.array(modified_mask_image), (blur_kernel_size, blur_kernel_size), 0)  # 高斯模糊
    #mask_array = np.array(modified_mask_image)
    mask_image = Image.fromarray(mask_array)  # 将模糊后的掩码转换回 PIL 图像
    
    # 将裁剪的面部图像粘贴回扩展后的面部区域
    face_large.paste(face, (x - x_s, y - y_s, x1 - x_s, y1 - y_s))
    
    body.paste(face_large, crop_box[:2], mask_image)
    
    body = np.array(body)  # 将 PIL 图像转换回 numpy 数组

    return body[:, :, ::-1]  # 返回处理后的图像（BGR 转 RGB）


def get_image_blending(image, face, face_box, mask_array, crop_box):
    body = Image.fromarray(image[:,:,::-1])
    face = Image.fromarray(face[:,:,::-1])

    x, y, x1, y1 = face_box
    x_s, y_s, x_e, y_e = crop_box
    face_large = body.crop(crop_box)

    mask_image = Image.fromarray(mask_array)
    mask_image = mask_image.convert("L")
    face_large.paste(face, (x-x_s, y-y_s, x1-x_s, y1-y_s))
    body.paste(face_large, crop_box[:2], mask_image)
    body = np.array(body)
    return body[:,:,::-1]


def get_image_prepare_material(image, face_box, upper_boundary_ratio=0.5, expand=1.5, fp=None, mode="raw"):
    body = Image.fromarray(image[:,:,::-1])

    x, y, x1, y1 = [int(v) for v in face_box]  # 1003fix: 卡通coords float守卫
    #print(x1-x,y1-y)
    crop_box, s = get_crop_box(face_box, expand)
    # 1003fix(卡通形象): 检测框常贴到画面边, 外扩crop_box越界 → PIL crop 出0尺寸 → resize崩
    # (height and width must be > 0, 整条genavatar死)。越界clamp进画面, 掩码按原位pad回全尺寸。
    W, H = body.size
    cx1 = max(0, min(crop_box[0], W - 1)); cy1 = max(0, min(crop_box[1], H - 1))
    cx2 = max(cx1 + 1, min(crop_box[2], W)); cy2 = max(cy1 + 1, min(crop_box[3], H))
    crop_box_clamped = [cx1, cy1, cx2, cy2]
    ox, oy = crop_box[0] - cx1, crop_box[1] - cy1  # 原crop_box坐标系与clamp后系的偏移

    face_large = body.crop(crop_box_clamped)
    ori_shape = face_large.size

    mask_image = face_seg(face_large, mode=mode, fp=fp)
    mask_small = mask_image.crop((x-cx1, y-cy1, x1-cx1, y1-cy1))
    mask_image = Image.new('L', ori_shape, 0)
    mask_image.paste(mask_small, (x-cx1, y-cy1, x1-cx1, y1-cy1))

    # keep upper_boundary_ratio of talking area
    width, height = mask_image.size
    top_boundary = int(height * upper_boundary_ratio)
    modified_mask_image = Image.new('L', ori_shape, 0)
    modified_mask_image.paste(mask_image.crop((0, top_boundary, width, height)), (0, top_boundary))

    blur_kernel_size = int(0.1 * ori_shape[0] // 2 * 2) + 1
    mask_array = cv2.GaussianBlur(np.array(modified_mask_image), (blur_kernel_size, blur_kernel_size), 0)
    # 1008fix(铁律): 必须返回 clamp 后的 crop_box。上面 mask 是按 ori_shape(=clamp后
    # face_large 尺寸)生成的, 而原 crop_box 在脸框贴边时是越界值 -> 写入 mask_coords.pkl
    # 后与 mask PNG 尺寸失配(如 600x600 vs 600x559) -> 运行时 get_image_blending 的
    # body.crop(crop_box) 与 mask_array 错位 -> 嘴糊/丢帧(r3 故障 + r5 246/246 全失配)。
    # 界内素材 clamp 前后相同, 故对已验收包零影响。
    return mask_array, crop_box_clamped
