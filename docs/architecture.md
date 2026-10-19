# 架构与代码导航

`main.py` 创建 QApplication，加载配置并打开主窗口。

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 数据 | core/models.py | 照片文件、分组、审核状态 |
| 扫描 | core/scanner.py、metadata.py | 同名分组、拍摄时间、XMP 评分 |
| 预览 | core/imageloader.py、exif.py | JPG 解码、缓存、后台预取、曝光信息 |
| 操作 | core/fileops.py、trash.py、undo.py | 串行文件操作、校验、manifest、撤销 |
| 筛选 | core/filters.py | 日期与评分多选的组合逻辑 |
| 界面 | ui/mainwindow.py、trashwindow.py | 浏览交互和回收站管理 |

后台线程传递 QImage 或普通数据；界面更新由主线程处理。文件操作通过队列串行执行。
配置与照片数据留在用户本机，仓库只保存源码与合成测试。
