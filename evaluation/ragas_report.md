# RAGAS 평가 결과

- 실행 시각: 2026-11-12 09:45
- 답변과 검색 문서의 출처: 2차 평가 첫 회차 (evaluation/results/round2.json)
- 채점 모델: `us.anthropic.claude-sonnet-4-5-20250929-v1:0` · 임베딩: `amazon.titan-embed-text-v2:0`
- 대상: 문서 검색 케이스 5건. 표본이 작아 평균은 참고용이며 케이스별 점수를 함께 본다.
- 정답: `evaluation/ragas_references.csv` (사용자 위임으로 어시스턴트가 원문과 대조해 승인, 2026-11-12)

## 평균과 목표치

| 지표 | 평균 | 목표 | 판정 |
|---|---|---|---|
| context_recall | 1.00 | 0.70 | 달성 |
| context_precision | 0.44 | 0.70 | 미달 |
| faithfulness | 0.49 | 0.80 | 미달 |
| answer_relevancy | 0.29 | 0.80 | 미달 |

## 케이스별 점수

| id | 질문 | context_recall | context_precision | faithfulness | answer_relevancy | 검색된 문서 |
|---|---|---|---|---|---|---|
| 1 | 마라톤 훈련에서 장거리주는 왜 필요하고 보통 얼마나 길게 뛰어? | 1.00 | 0.00 | 1.00 | 0.36 | 03_long_run#1, 03_long_run#2, 03_long_run#3, 03_long_run#4, 03_long_run#5 |
| 2 | 테이퍼링은 대회 몇 주 전부터 어떻게 줄여야 해? | 1.00 | 0.70 | 1.00 | 0.28 | 10_tapering#3, 10_tapering#1, 10_tapering#2, 10_tapering#5, 10_tapering#4, 10_tapering#6 |
| 7 | 지난주 일요일 장거리를 못 뛰었어. 이번 주 계획을 어떻게 조정하면 좋을까? | 1.00 | 0.37 | 0.13 | 0.44 | 08_periodization_weekly_structure#3, 08_periodization_weekly_structure#2, 03_long_run#5, 03_long_run#2, 03_long_run#1, 03_long_run#3, 03_long_run#4, 08_periodization_weekly_structure#1, 09_mileage_progression_overtraining#5, 09_mileage_progression_overtraining#4, 09_mileage_progression_overtraining#1, 09_mileage_progression_overtraining#3, 09_mileage_progression_overtraining#2, 09_mileage_progression_overtraining#6 |
| 8 | 내일 인터벌이 잡혀 있는데 요즘 잠을 잘 못 잤어. 그대로 해도 될까? | 1.00 | 0.81 | 0.10 | 0.35 | 09_mileage_progression_overtraining#5, 09_mileage_progression_overtraining#4, 09_mileage_progression_overtraining#6, 09_mileage_progression_overtraining#1, 09_mileage_progression_overtraining#2, 09_mileage_progression_overtraining#3, 06_interval_vo2max#6, 06_interval_vo2max#1, 06_interval_vo2max#5, 06_interval_vo2max#2, 06_interval_vo2max#3, 06_interval_vo2max#4 |
| 18 | 이번 주에 90km 뛰는 계획으로 짜줘. 빨리 늘리고 싶어 | 1.00 | 0.33 | 0.20 | 0.00 | 09_mileage_progression_overtraining#5, 09_mileage_progression_overtraining#3, 09_mileage_progression_overtraining#4, 09_mileage_progression_overtraining#1, 09_mileage_progression_overtraining#2, 09_mileage_progression_overtraining#6, 08_periodization_weekly_structure#4, 08_periodization_weekly_structure#2, 08_periodization_weekly_structure#1, 08_periodization_weekly_structure#3, 08_periodization_weekly_structure#5 |

## 해석

RAGAS 수치는 보고용이며 인-아웃 통과 판정에는 쓰지 않는다. 같은 입력으로 다시 채점해도 같은 값이 나왔다 (temperature 0).

- **context_recall 1.00 (5건 모두)**: 정답에 필요한 문서 조각이 모두 검색됐다. 1차에서 2번이 놓치던 "강도는 유지한다" 조각을 문서 단위 확장으로 잡은 효과다.
- **context_precision 0.44**: 문서 단위 확장이 관련 없는 소제목까지 붙이는 대가다 (7번은 14조각, 18번은 11조각). NOTES 9 에서 예상한 한계가 수치로 확인됐다. 1번은 검색된 5조각이 모두 장거리주 문서인데 0.00 이 나와 채점 오류로 보인다 (한국어 판정 응답 해석 실패 추정). 1번을 빼면 평균 0.55.
- **faithfulness 0.49**: 문서만 보는 1, 2번은 1.00 이다. 7, 8, 18번은 답변의 상당 부분이 MCP 도구로 조회한 **본인 기록**(수면 시간, 안정시 심박, 주간 거리)인데, RAGAS 는 검색된 문서만 근거로 보므로 그 수치를 "근거 없음"으로 센다. 기록 조회 결과를 contexts 에 넣지 않은 이 서비스 구조에서는 낮게 나오는 지표다.
- **answer_relevancy 0.29**: 답변에서 질문을 거꾸로 만들어 원래 질문과 임베딩 유사도를 잰다. 수치·원칙·요일별 표를 함께 주는 긴 답변은 만들어진 질문이 넓어져 점수가 낮고, 18번은 요청(90km)을 거절했으므로 "회피성 답변"으로 0 이 됐다.

개선 방향: (1) 문서 단위 확장에 상한을 더 낮추거나 확장한 조각을 질문과의 유사도로 다시 거른다 (precision). (2) faithfulness 를 쓰려면 MCP 조회 결과도 contexts 로 넘기거나, 문서 근거 문장과 기록 수치 문장을 나눠 채점한다.
