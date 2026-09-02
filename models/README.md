# 모델 가중치

용량이 커서 Git에 커밋하지 않는다 (`.gitignore` 참조).

## 필요한 파일

| 파일 | 용도 | 출처 |
| :--- | :--- | :--- |
| `yolov8n.onnx` | 사람 검출 (WBS 3.3.1) | `ros2_amr_fleet_control` 프로젝트 자산 이관 |

## 배치

```
models/yolov8n.onnx
```

경로는 `config/config.yaml` 의 `vision.model_path` 에서 관리한다.
