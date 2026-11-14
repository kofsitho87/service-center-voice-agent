"""Golden snapshot for test_b_search_schedules_characterization.

Captured from the pre-refactor DynamicBookingTask implementation.
Regenerate only for an intentional behavior change."""

GOLDEN_JSON = r"""
{
 "awaiting_after_empty_same_turn": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 0
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "availability": "none_in_searched_range",
    "date": "2026-09-09",
    "date_spoken": "9월 9일 수요일",
    "request_match": {
     "alternative_slot_count": 0,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "can_search_later_dates": true,
     "confirmation_allowed": false,
     "handoff_available": false,
     "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 아닙니다. 다음 순서로 하나를 고르세요. 1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하세요. handoff_available=false이면 시간을 유지해 다른 날짜를 찾을지 물으세요. 2. 시간 조건 없이 조회한 날짜 전체가 비었으므로 같은 날 다른 시간·시간대를 제안하거나 묻지 말고, 다른 날짜를 찾을지 물으세요. other_advisors_available=true이면 같은 날 다른 상담사는 가능하다고 안내하고 그렇게 찾을지도 함께 물으세요. 답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. 다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. ",
     "wait_for_customer": true
    },
    "search_horizon_reached": false,
    "searched_range": {
     "end_date": "2026-09-09",
     "spoken": "9월 9일 수요일",
     "start_date": "2026-09-09"
    },
    "slots": [],
    "status": "ok"
   },
   {
    "code": "awaiting_customer_after_no_availability",
    "message": "먼저 previous_search의 기간과 시간 조건에 일정이 없다고 안내하세요. 이전 required_next_action을 따라 대안을 물은 뒤 고객의 다음 답변을 기다리세요. 검색이나 후보 검증을 반복하지 마세요.",
    "previous_search": {
     "availability": "none_in_searched_range",
     "date": "2026-09-09",
     "date_spoken": "9월 9일 수요일",
     "request_match": {
      "alternative_slot_count": 0,
      "matching_slot_count": 0
     },
     "requested_range_complete": true,
     "required_next_action": {
      "can_search_later_dates": true,
      "confirmation_allowed": false,
      "handoff_available": false,
      "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 아닙니다. 다음 순서로 하나를 고르세요. 1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하세요. handoff_available=false이면 시간을 유지해 다른 날짜를 찾을지 물으세요. 2. 시간 조건 없이 조회한 날짜 전체가 비었으므로 같은 날 다른 시간·시간대를 제안하거나 묻지 말고, 다른 날짜를 찾을지 물으세요. other_advisors_available=true이면 같은 날 다른 상담사는 가능하다고 안내하고 그렇게 찾을지도 함께 물으세요. 답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. 다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. ",
      "wait_for_customer": true
     },
     "search_horizon_reached": false,
     "searched_range": {
      "end_date": "2026-09-09",
      "spoken": "9월 9일 수요일",
      "start_date": "2026-09-09"
     },
     "slots": [],
     "status": "ok"
    },
    "status": "awaiting_customer"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [
    "2026-09-09~2026-09-09"
   ],
   "_last_presented": [],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-09",
    "operator_id": null,
    "start_date": "2026-09-09",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 0,
    "filters": {
     "service_id": 3590
    },
    "horizon_reached": false,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-09-09",
     "2026-09-09"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-09",
     "operator_id": null,
     "start_date": "2026-09-09",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "caller_date_correction": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 3,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-08-28",
    "date_corrected": {
     "from": "2026-08-29",
     "instruction": "고객이 말한 '내일'은 2026-08-28(금요일)이라 그 날짜로 조회했습니다. 2026-08-29은 언급하지 말고 이 결과의 spoken 문구로 안내하세요.",
     "phrase": "내일",
     "to": "2026-08-28"
    },
    "date_spoken": "8월 28일 금요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 3,
     "total_available": 3
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 11:00",
      "advisors": [
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 11시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 15:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오후 3시",
      "weekday": "금"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-28 09:00",
    "2026-08-28 11:00",
    "2026-08-28 15:00"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "declined_alternatives_offer": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 3,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 2,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 1,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 1,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-08-28",
    "date_spoken": "8월 28일 금요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 3,
     "total_available": 3
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 11:00",
      "advisors": [
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 11시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 15:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오후 3시",
      "weekday": "금"
     }
    ],
    "status": "ok"
   },
   {
    "date": "2026-08-31",
    "date_spoken": "8월 31일 월요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 2,
     "total_available": 2
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-31",
     "spoken": "8월 31일 월요일",
     "start_date": "2026-08-31"
    },
    "slots": [
     {
      "datetime": "2026-08-31 10:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 31일 월요일 오전 10시",
      "weekday": "월"
     },
     {
      "datetime": "2026-08-31 10:30",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 31일 월요일 오전 10시 30분",
      "weekday": "월"
     }
    ],
    "status": "ok"
   },
   {
    "date": "2026-09-01",
    "date_spoken": "9월 1일 화요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 1,
     "total_available": 1
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "instruction": "지금 같은 응답에서 stage_args 그대로 stage_booking을 호출하세요. 호출 전 발화·새 동의 질문·턴 종료는 금지합니다. 성공 후 한 발화로 답하세요. 고객이 가능 여부를 물었다면 '네, 가능합니다'처럼 그 답을 먼저 짧게 말하고, 이어서 반환된 confirmation의 전체 서비스명·날짜·시간과 제공된 상담사명으로 예약 신청 여부를 한 번 묻고 고객의 새 명시적 동의를 기다리세요. stage_booking은 예약 신청이 아니므로 commit_booking을 연속 호출하지 마세요.",
     "required_tool": "stage_booking",
     "stage_args": {
      "datetime": "2026-09-01 16:00",
      "service_id": 3590
     }
    },
    "searched_range": {
     "end_date": "2026-09-01",
     "spoken": "9월 1일 화요일",
     "start_date": "2026-09-01"
    },
    "slots": [
     {
      "datetime": "2026-09-01 16:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       },
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "9월 1일 화요일 오후 4시",
      "weekday": "화"
     }
    ],
    "status": "ok"
   },
   {
    "date": "2026-09-02",
    "date_spoken": "9월 2일 수요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 1,
     "total_available": 1
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "instruction": "지금 같은 응답에서 stage_args 그대로 stage_booking을 호출하세요. 호출 전 발화·새 동의 질문·턴 종료는 금지합니다. 성공 후 한 발화로 답하세요. 고객이 가능 여부를 물었다면 '네, 가능합니다'처럼 그 답을 먼저 짧게 말하고, 이어서 반환된 confirmation의 전체 서비스명·날짜·시간과 제공된 상담사명으로 예약 신청 여부를 한 번 묻고 고객의 새 명시적 동의를 기다리세요. stage_booking은 예약 신청이 아니므로 commit_booking을 연속 호출하지 마세요.",
     "required_tool": "stage_booking",
     "stage_args": {
      "datetime": "2026-09-02 16:00",
      "service_id": 3590
     }
    },
    "searched_range": {
     "end_date": "2026-09-02",
     "spoken": "9월 2일 수요일",
     "start_date": "2026-09-02"
    },
    "slots": [
     {
      "datetime": "2026-09-02 16:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       },
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "9월 2일 수요일 오후 4시",
      "weekday": "수"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": "u7",
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-09-02 16:00"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-02",
    "operator_id": null,
    "start_date": "2026-09-02",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 4,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 3,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": false,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-02",
     "operator_id": null,
     "start_date": "2026-09-02",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "earliest_for_booking_requires_service": {
  "events": [],
  "outputs": [
   {
    "code": "invalid_arguments",
    "message": "mode=earliest_for_booking requires service_id.",
    "retry": "silent",
    "status": "error"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": null,
   "_last_search_conditions": null,
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {}
   }
  }
 },
 "earliest_offer_then_stage": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 13
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 1,
      "service_id": 4720,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   },
   {
    "agent_event": {
     "details": {
      "datetime": "2026-08-28 09:30",
      "service_id": 4720,
      "operator_assigned": true,
      "operator_id": 806
     },
     "name": "BOOKING_CREATE",
     "status": "STAGED"
    }
   }
  ],
  "outputs": [
   {
    "earliest_only_truncated": {
     "instruction": "같은 조건에 더 늦은 시각이 남아 있습니다. 이 슬롯을 그 날의 유일한 선택지로 말하지 마세요. 고객이 다른 시각을 물으면 strictly_after로 다시 조회하세요.",
     "omitted_later_slots": 1,
     "returned": 1
    },
    "requested_range_complete": false,
    "required_next_action": {
     "confirmation_allowed": false,
     "instruction": "지금 같은 응답에서 stage_args 그대로 stage_booking을 호출하세요. 호출 전 발화·새 동의 질문·턴 종료는 금지합니다. 성공 후 한 발화로 답하세요. 고객이 가능 여부를 물었다면 '네, 가능합니다'처럼 그 답을 먼저 짧게 말하고, 이어서 반환된 confirmation의 전체 서비스명·날짜·시간과 제공된 상담사명으로 예약 신청 여부를 한 번 묻고 고객의 새 명시적 동의를 기다리세요. stage_booking은 예약 신청이 아니므로 commit_booking을 연속 호출하지 마세요.",
     "required_tool": "stage_booking",
     "stage_args": {
      "datetime": "2026-08-28 09:30",
      "service_id": 4720
     }
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:30",
      "advisors": [
       {
        "id": 806,
        "name": "이테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시 30분",
      "weekday": "금"
     }
    ],
    "status": "ok"
   },
   {
    "confirmation": {
     "date": "2026-08-28",
     "datetime": "2026-08-28 09:30",
     "service_name": "노트북 상담",
     "is_earliest": true,
     "language": "ko-KR",
     "time": "09:30",
     "weekday_iso": 5
    },
    "confirmation_context": "earliest_offer",
    "required_next_action": "ask_booking_confirmation",
    "staged": {
     "datetime": "2026-08-28 09:30",
     "service_id": 4720,
     "service_name": "노트북 상담",
     "operator_id": 806,
     "operator_name": "이테스트"
    },
    "status": "staged"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": null,
   "_last_search_conditions": {
    "service_id": 4720,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 2,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": {
    "__dataclass__": "StagedBooking",
    "datetime": "2026-08-28 09:30",
    "service_id": 4720,
    "service_name": "노트북 상담",
    "operator_id": 806,
    "operator_name": "이테스트"
   },
   "pending": {
    "__dataclass__": "DynamicBookingPending",
    "datetime": "2026-08-28 09:30",
    "service_id": 4720,
    "service_name": "노트북 상담",
    "operator_id": 806,
    "operator_name": "이테스트"
   },
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 4720,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "invalid_range": {
  "events": [],
  "outputs": [
   {
    "allowed_range": "2026-08-27 ~ 2026-09-26",
    "code": "out_of_horizon",
    "message": "조회 가능한 범위를 벗어났습니다.",
    "retry": "silent",
    "status": "error"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": null,
   "_last_search_conditions": null,
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {}
   }
  }
 },
 "invalid_time": {
  "events": [],
  "outputs": [
   {
    "code": "invalid_time",
    "message": "time_from은 HH:MM 형식이어야 합니다.",
    "retry": "silent",
    "status": "error"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": null,
   "_last_search_conditions": null,
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {}
   }
  }
 },
 "invalid_time_order": {
  "events": [],
  "outputs": [
   {
    "code": "invalid_time_range",
    "message": "time_from must not be later than time_to. Preserve the caller's constraints and clarify the intended window before retrying.",
    "retry": "clarify",
    "status": "error"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": null,
   "_last_search_conditions": null,
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {}
   }
  }
 },
 "multi_earliest_empty": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 1
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "availability": "none_in_searched_range",
    "request_match": {
     "alternative_slot_count": 0,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "can_search_later_dates": false,
     "confirmation_allowed": false,
     "handoff_available": false,
     "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. 조회 가능한 마지막 날짜까지 확인했습니다. 더 이후의 날짜를 검색하거나 제안하지 마세요. ",
     "wait_for_customer": true
    },
    "search_horizon_reached": true,
    "searched_range": {
     "end_date": "2026-09-26",
     "spoken": "9월 9일 수요일부터 9월 26일 토요일까지",
     "start_date": "2026-09-09"
    },
    "slots": [],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [
    "2026-09-09~2026-09-26"
   ],
   "_last_presented": [],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-26",
    "operator_id": null,
    "start_date": "2026-09-09",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 0,
    "filters": {
     "service_id": 3590
    },
    "horizon_reached": true,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-09-09",
     "2026-09-26"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-26",
     "operator_id": null,
     "start_date": "2026-09-09",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "multi_earliest_for_booking": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 13
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 1,
      "service_id": 4720,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "earliest_only_truncated": {
     "instruction": "같은 조건에 더 늦은 시각이 남아 있습니다. 이 슬롯을 그 날의 유일한 선택지로 말하지 마세요. 고객이 다른 시각을 물으면 strictly_after로 다시 조회하세요.",
     "omitted_later_slots": 1,
     "returned": 1
    },
    "requested_range_complete": false,
    "required_next_action": {
     "confirmation_allowed": false,
     "instruction": "지금 같은 응답에서 stage_args 그대로 stage_booking을 호출하세요. 호출 전 발화·새 동의 질문·턴 종료는 금지합니다. 성공 후 한 발화로 답하세요. 고객이 가능 여부를 물었다면 '네, 가능합니다'처럼 그 답을 먼저 짧게 말하고, 이어서 반환된 confirmation의 전체 서비스명·날짜·시간과 제공된 상담사명으로 예약 신청 여부를 한 번 묻고 고객의 새 명시적 동의를 기다리세요. stage_booking은 예약 신청이 아니므로 commit_booking을 연속 호출하지 마세요.",
     "required_tool": "stage_booking",
     "stage_args": {
      "datetime": "2026-08-28 09:30",
      "service_id": 4720
     }
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:30",
      "advisors": [
       {
        "id": 806,
        "name": "이테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시 30분",
      "weekday": "금"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-28 09:30"
   ],
   "_last_search_conditions": {
    "service_id": 4720,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": {
    "__dataclass__": "EarliestOffer",
    "datetime": "2026-08-28 09:30",
    "service_id": 4720,
    "operator_id": null
   },
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 4720,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "multi_earliest_horizon_clamp": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 13
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 1,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "earliest_only_truncated": {
     "instruction": "같은 조건에 더 늦은 시각이 남아 있습니다. 이 슬롯을 그 날의 유일한 선택지로 말하지 마세요. 고객이 다른 시각을 물으면 strictly_after로 다시 조회하세요.",
     "omitted_later_slots": 2,
     "returned": 1
    },
    "range_adjustment": {
     "applied_end_date": "2026-09-26",
     "reason": "booking_horizon",
     "requested_end_date": "2026-10-30"
    },
    "requested_range_complete": false,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시",
      "weekday": "금"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": null,
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": {
    "__dataclass__": "EarliestOffer",
    "datetime": "2026-08-28 09:00",
    "service_id": 3590,
    "operator_id": null
   },
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "multi_earliest_target_nearby": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 3
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-09-01",
    "exact_time_match": false,
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 1,
     "total_available": 1
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "handoff_available": false,
     "instruction": "searched_range에서 요청한 정확한 시각에는 자리가 없다고 안내하고, slots에 있는 그날의 가까운 시간만 가능한 대안으로 제시하세요. 고객이 이미 말한 시간이 slots에 있으면 그 시간을 우선 짚으세요. 다른 날짜를 먼저 제안하지 마세요. 고객이 그 시간만 가능하다고 명시했다면 같은 날 대안을 권하지 말고, handoff_available=true이면 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하고 아니면 다른 날짜를 찾을지 물으세요. 고객이 대안을 선택하기 전에는 stage_booking이나 예약 신청 확인 질문을 하지 말고 답변을 기다리세요.",
     "wait_for_customer": true
    },
    "searched_range": {
     "end_date": "2026-09-03",
     "spoken": "9월 1일 화요일부터 9월 3일 목요일까지",
     "start_date": "2026-09-01"
    },
    "searched_time_window": {
     "from": "15:30",
     "instruction": "이 결과는 위 시각 범위만 확인했습니다. 범위 밖 시각을 고객이 물으면 가능·불가를 말하기 전에 그 시각으로 search_schedules를 다시 호출하세요.",
     "to": "15:30"
    },
    "slots": [
     {
      "datetime": "2026-09-01 16:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       },
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "9월 1일 화요일 오후 4시",
      "weekday": "화"
     }
    ],
    "status": "ok",
    "target_time": "15:30"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": null,
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-03",
    "operator_id": null,
    "start_date": "2026-09-01",
    "strictly_after": null,
    "target_time": "15:30",
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-03",
     "operator_id": null,
     "start_date": "2026-09-01",
     "strictly_after": null,
     "target_time": "15:30",
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "multi_empty_through_horizon_offer": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 1
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "blocker": {
       "alternative_slot_count": 0,
       "filters": {
        "service_id": 3590
       },
       "horizon_reached": true,
       "kind": "no_matching_slot",
       "matching_slot_count": 0,
       "range_complete": true,
       "searched_range": {
        "end_date": "2026-09-26",
        "start_date": "2026-09-09"
       }
      },
      "blocker_kind": "no_matching_slot",
      "handoff_channel": "memo",
      "offer_id": "offer0000000",
      "reason_code": "no_slot_memo"
     },
     "name": "BOOKING_CREATE",
     "status": "HANDOFF_OFFERED"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "availability": "none_in_searched_range",
    "available_dates": [],
    "handoff_offer": {
     "ask": "Say in one short sentence what could not be matched, using only the checked facts, then ask one yes/no question whether to leave a message for the staff. Ask nothing else in this turn, and wait for the answer.",
     "channel": "memo",
     "reason": "caller_condition_not_bookable"
    },
    "request_match": {
     "alternative_slot_count": 0,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "can_search_later_dates": false,
     "confirmation_allowed": false,
     "handoff_available": true,
     "instruction": "Say what the checked range and filters found, then ask only the handoff_offer question instead of proposing another search.",
     "wait_for_customer": true
    },
    "search_horizon_reached": true,
    "searched_range": {
     "end_date": "2026-09-26",
     "spoken": "9월 9일 수요일부터 9월 26일 토요일까지",
     "start_date": "2026-09-09"
    },
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [
    "2026-09-09~2026-09-26"
   ],
   "_last_presented": [],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-26",
    "operator_id": null,
    "start_date": "2026-09-09",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": {
    "__dataclass__": "HandoffOffer",
    "blocker": {
     "__dataclass__": "NoMatchingSlotBlocker",
     "alternative_slot_count": 0,
     "filters": {
      "service_id": 3590
     },
     "horizon_reached": true,
     "kind": "no_matching_slot",
     "range_complete": true,
     "searched_range": [
      "2026-09-09",
      "2026-09-26"
     ]
    },
    "channel": "memo",
    "id": "offer0000000",
    "reason_code": "no_slot_memo",
    "revision": 1,
    "user_message_id": "u1"
   },
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 0,
    "filters": {
     "service_id": 3590
    },
    "horizon_reached": true,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-09-09",
     "2026-09-26"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-26",
     "operator_id": null,
     "start_date": "2026-09-09",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "multi_filtered_empty": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 7
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 1
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "availability": "none_in_searched_range",
    "available_dates": [],
    "request_match": {
     "alternative_slot_count": 0,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "can_search_later_dates": true,
     "confirmation_allowed": false,
     "handoff_available": false,
     "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 아닙니다. 다음 순서로 하나를 고르세요. 1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하세요. handoff_available=false이면 시간을 유지해 다른 날짜를 찾을지 물으세요. 2. 그런 명시적 제한 없이 상담사만 바꿨다면 '같은 날 다른 시간도 괜찮으세요?'라고 물으세요. 날짜 변경을 먼저 제안하지 마세요. 3. 그 외에는 고객이 밝힌 선호를 따르고, 불분명하면 조정 가능한 조건 하나를 질문하세요. 답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. 다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. ",
     "wait_for_customer": true
    },
    "search_horizon_reached": false,
    "searched_range": {
     "end_date": "2026-09-04",
     "spoken": "8월 29일 토요일부터 9월 4일 금요일까지",
     "start_date": "2026-08-29"
    },
    "searched_time_window": {
     "from": "00:00",
     "instruction": "이 결과는 위 시각 범위만 확인했습니다. 범위 밖 시각을 고객이 물으면 가능·불가를 말하기 전에 그 시각으로 search_schedules를 다시 호출하세요.",
     "to": "08:00"
    },
    "status": "ok",
    "time_from": null,
    "time_to": "08:00"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [
    "2026-08-29~2026-09-04"
   ],
   "_last_presented": [],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-04",
    "operator_id": null,
    "start_date": "2026-08-29",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": "08:00"
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 0,
    "filters": {
     "service_id": 3590,
     "time_to": "08:00"
    },
    "horizon_reached": false,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-08-29",
     "2026-09-04"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-04",
     "operator_id": null,
     "start_date": "2026-08-29",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": "08:00"
    }
   }
  }
 },
 "multi_filtered_limit_mid_chunk": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 7
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 4,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "available_dates": [
     {
      "date": "2026-09-01",
      "latest_appointment_time": "16:00",
      "spoken": "9월 1일 화요일",
      "weekday": "화"
     },
     {
      "date": "2026-09-02",
      "latest_appointment_time": "16:00",
      "spoken": "9월 2일 수요일",
      "weekday": "수"
     },
     {
      "date": "2026-09-03",
      "latest_appointment_time": "16:00",
      "spoken": "9월 3일 목요일",
      "weekday": "목"
     }
    ],
    "latest_time_comparison": {
     "dates": [
      "2026-09-01",
      "2026-09-02",
      "2026-09-03"
     ],
     "scope": "returned_dates",
     "time": "16:00"
    },
    "presentation": {
     "has_more": true,
     "instruction": "available_dates에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "date",
     "remaining_count": 1,
     "returned": 3,
     "total_available": 4
    },
    "range_adjustment": {
     "applied_end_date": "2026-09-07",
     "reason": "time_search_window",
     "requested_end_date": "2026-09-08"
    },
    "requested_range_complete": false,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 날짜를 선택한 뒤 그 날짜의 정확한 시간을 search_schedules로 조회하세요. 아직 stage_booking을 호출하지 마세요.",
      "tool": "search_schedules",
      "when": "after_customer_selects_date"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요. latest_time_comparison은 반환된 후보 날짜와 적용된 시간 조건 안에서 마지막 예약 가능 시각을 비교한 값입니다. 시간 조건 밖의 그날 최댓값, 영업 종료시간, 미조회 날짜의 최댓값이 아닙니다. 가장 늦은 시각을 물으면 이 비교의 시각과 동률 날짜를 안내하세요. 시각 비교 요청만으로 예약 확인 질문을 하거나 stage_booking을 호출하지 말고 고객이 날짜·시간을 선택할 때까지 기다리세요."
    },
    "searched_range": {
     "end_date": "2026-09-05",
     "spoken": "9월 1일 화요일부터 9월 5일 토요일까지",
     "start_date": "2026-09-01"
    },
    "searched_time_window": {
     "from": "15:00",
     "instruction": "이 결과는 위 시각 범위만 확인했습니다. 범위 밖 시각을 고객이 물으면 가능·불가를 말하기 전에 그 시각으로 search_schedules를 다시 호출하세요.",
     "to": "23:59"
    },
    "status": "ok",
    "time_from": "15:00",
    "time_to": null
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-09-01",
    "2026-09-02",
    "2026-09-03"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-05",
    "operator_id": null,
    "start_date": "2026-09-01",
    "strictly_after": null,
    "target_time": null,
    "time_from": "15:00",
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-05",
     "operator_id": null,
     "start_date": "2026-09-01",
     "strictly_after": null,
     "target_time": null,
     "time_from": "15:00",
     "time_to": null
    }
   }
  }
 },
 "multi_filtered_target_nearby": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 5
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-09-01",
    "exact_time_match": false,
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 1,
     "total_available": 1
    },
    "request_match": {
     "alternative_slot_count": 1,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "handoff_available": false,
     "instruction": "searched_range에서 요청한 정확한 시각에는 자리가 없다고 안내하고, slots에 있는 그날의 가까운 시간만 가능한 대안으로 제시하세요. 고객이 이미 말한 시간이 slots에 있으면 그 시간을 우선 짚으세요. 다른 날짜를 먼저 제안하지 마세요. 고객이 그 시간만 가능하다고 명시했다면 같은 날 대안을 권하지 말고, handoff_available=true이면 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하고 아니면 다른 날짜를 찾을지 물으세요. 고객이 대안을 선택하기 전에는 stage_booking이나 예약 신청 확인 질문을 하지 말고 답변을 기다리세요.",
     "wait_for_customer": true
    },
    "searched_range": {
     "end_date": "2026-09-05",
     "spoken": "9월 1일 화요일부터 9월 5일 토요일까지",
     "start_date": "2026-09-01"
    },
    "searched_time_window": {
     "from": "15:00",
     "instruction": "이 결과는 위 시각 범위만 확인했습니다. 범위 밖 시각을 고객이 물으면 가능·불가를 말하기 전에 그 시각으로 search_schedules를 다시 호출하세요.",
     "to": "15:00"
    },
    "slots": [
     {
      "datetime": "2026-09-01 16:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       },
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "9월 1일 화요일 오후 4시",
      "weekday": "화"
     }
    ],
    "status": "ok",
    "target_time": "15:00"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-09-01 16:00"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-05",
    "operator_id": null,
    "start_date": "2026-09-01",
    "strictly_after": null,
    "target_time": "15:00",
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 1,
    "filters": {
     "service_id": 3590,
     "target_time": "15:00"
    },
    "horizon_reached": false,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-09-01",
     "2026-09-05"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-05",
     "operator_id": null,
     "start_date": "2026-09-01",
     "strictly_after": null,
     "target_time": "15:00",
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "multi_operator_other_advisor_dates": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": 805
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "availability": "none_in_searched_range",
    "available_dates": [],
    "other_advisors_available": true,
    "request_match": {
     "alternative_slot_count": 1,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "can_search_later_dates": true,
     "confirmation_allowed": false,
     "handoff_available": false,
     "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 아닙니다. 다음 순서로 하나를 고르세요. 1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하세요. handoff_available=false이면 시간을 유지해 다른 날짜를 찾을지 물으세요. 2. 시간 조건 없이 조회한 날짜 전체가 비었으므로 같은 날 다른 시간·시간대를 제안하거나 묻지 말고, 다른 날짜를 찾을지 물으세요. other_advisors_available=true이면 같은 날 다른 상담사는 가능하다고 안내하고 그렇게 찾을지도 함께 물으세요. 답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. 다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. ",
     "wait_for_customer": true
    },
    "search_horizon_reached": false,
    "searched_range": {
     "end_date": "2026-08-30",
     "spoken": "8월 29일 토요일부터 8월 30일 일요일까지",
     "start_date": "2026-08-29"
    },
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [
    "2026-08-29~2026-08-30"
   ],
   "_last_presented": [],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-30",
    "operator_id": 805,
    "start_date": "2026-08-29",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 1,
    "filters": {
     "service_id": 3590,
     "operator_id": 805
    },
    "horizon_reached": false,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-08-29",
     "2026-08-30"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-30",
     "operator_id": 805,
     "start_date": "2026-08-29",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "multi_operator_other_advisor_hint_time": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": 805
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "availability": "none_in_searched_range",
    "available_dates": [],
    "other_advisors_may_be_available": true,
    "request_match": {
     "alternative_slot_count": 0,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "can_search_later_dates": true,
     "confirmation_allowed": false,
     "handoff_available": false,
     "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 아닙니다. 다음 순서로 하나를 고르세요. 1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하세요. handoff_available=false이면 시간을 유지해 다른 날짜를 찾을지 물으세요. 2. 그런 명시적 제한 없이 상담사만 바꿨다면 '같은 날 다른 시간도 괜찮으세요?'라고 물으세요. 날짜 변경을 먼저 제안하지 마세요. 3. 그 외에는 고객이 밝힌 선호를 따르고, 불분명하면 조정 가능한 조건 하나를 질문하세요. 답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. 다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. ",
     "wait_for_customer": true
    },
    "search_horizon_reached": false,
    "searched_range": {
     "end_date": "2026-08-30",
     "spoken": "8월 29일 토요일부터 8월 30일 일요일까지",
     "start_date": "2026-08-29"
    },
    "searched_time_window": {
     "from": "09:00",
     "instruction": "이 결과는 위 시각 범위만 확인했습니다. 범위 밖 시각을 고객이 물으면 가능·불가를 말하기 전에 그 시각으로 search_schedules를 다시 호출하세요.",
     "to": "23:59"
    },
    "status": "ok",
    "time_from": "09:00",
    "time_to": null
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [
    "2026-08-29~2026-08-30"
   ],
   "_last_presented": [],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-30",
    "operator_id": 805,
    "start_date": "2026-08-29",
    "strictly_after": null,
    "target_time": null,
    "time_from": "09:00",
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 0,
    "filters": {
     "service_id": 3590,
     "operator_id": 805,
     "time_from": "09:00"
    },
    "horizon_reached": false,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-08-29",
     "2026-08-30"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-30",
     "operator_id": 805,
     "start_date": "2026-08-29",
     "strictly_after": null,
     "target_time": null,
     "time_from": "09:00",
     "time_to": null
    }
   }
  }
 },
 "multi_plain_dates": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 13
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 11,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "available_dates": [
     {
      "date": "2026-08-28",
      "spoken": "8월 28일 금요일",
      "weekday": "금"
     },
     {
      "date": "2026-08-30",
      "spoken": "8월 30일 일요일",
      "weekday": "일"
     },
     {
      "date": "2026-08-31",
      "spoken": "8월 31일 월요일",
      "weekday": "월"
     }
    ],
    "presentation": {
     "has_more": true,
     "instruction": "available_dates에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "date",
     "remaining_count": 8,
     "returned": 3,
     "total_available": 11
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 날짜를 선택한 뒤 그 날짜의 정확한 시간을 search_schedules로 조회하세요. 아직 stage_booking을 호출하지 마세요.",
      "tool": "search_schedules",
      "when": "after_customer_selects_date"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-09-10",
     "spoken": "8월 28일 금요일부터 9월 10일 목요일까지",
     "start_date": "2026-08-28"
    },
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-28",
    "2026-08-30",
    "2026-08-31"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-10",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-10",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "multi_rejected_candidates": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 5
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 3,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "available_dates": [
     {
      "date": "2026-09-03",
      "latest_appointment_time": "16:00",
      "spoken": "9월 3일 목요일",
      "weekday": "목"
     },
     {
      "date": "2026-09-04",
      "latest_appointment_time": "16:00",
      "spoken": "9월 4일 금요일",
      "weekday": "금"
     },
     {
      "date": "2026-09-05",
      "latest_appointment_time": "16:00",
      "spoken": "9월 5일 토요일",
      "weekday": "토"
     }
    ],
    "latest_time_comparison": {
     "dates": [
      "2026-09-03",
      "2026-09-04",
      "2026-09-05"
     ],
     "scope": "returned_dates",
     "time": "16:00"
    },
    "presentation": {
     "has_more": false,
     "instruction": "available_dates에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "date",
     "remaining_count": 0,
     "returned": 3,
     "total_available": 3
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 날짜를 선택한 뒤 그 날짜의 정확한 시간을 search_schedules로 조회하세요. 아직 stage_booking을 호출하지 마세요.",
      "tool": "search_schedules",
      "when": "after_customer_selects_date"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요. latest_time_comparison은 반환된 후보 날짜와 적용된 시간 조건 안에서 마지막 예약 가능 시각을 비교한 값입니다. 시간 조건 밖의 그날 최댓값, 영업 종료시간, 미조회 날짜의 최댓값이 아닙니다. 가장 늦은 시각을 물으면 이 비교의 시각과 동률 날짜를 안내하세요. 시각 비교 요청만으로 예약 확인 질문을 하거나 stage_booking을 호출하지 말고 고객이 날짜·시간을 선택할 때까지 기다리세요."
    },
    "searched_range": {
     "end_date": "2026-09-05",
     "spoken": "9월 1일 화요일부터 9월 5일 토요일까지",
     "start_date": "2026-09-01"
    },
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-09-03",
    "2026-09-04",
    "2026-09-05"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-05",
    "operator_id": null,
    "start_date": "2026-09-01",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": [
    {
     "datetime": "2026-09-01 16:00",
     "reconsidered_datetimes": [],
     "scope": "slot",
     "user_message_id": "u0"
    },
    {
     "datetime": "2026-09-02 16:00",
     "reconsidered_datetimes": [],
     "scope": "date",
     "user_message_id": "u0"
    }
   ],
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [
     "2026-09-02"
    ],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [
     {
      "datetime": "2026-09-01 16:00",
      "reconsidered_datetimes": [],
      "scope": "slot",
      "user_message_id": "u0"
     },
     {
      "datetime": "2026-09-02 16:00",
      "reconsidered_datetimes": [],
      "scope": "date",
      "user_message_id": "u0"
     }
    ],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-05",
     "operator_id": null,
     "start_date": "2026-09-01",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "multi_strictly_after": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 3
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 2
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 3,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "available_dates": [
     {
      "date": "2026-08-31",
      "latest_appointment_time": "10:30",
      "spoken": "8월 31일 월요일",
      "weekday": "월"
     },
     {
      "date": "2026-09-01",
      "latest_appointment_time": "16:00",
      "spoken": "9월 1일 화요일",
      "weekday": "화"
     },
     {
      "date": "2026-09-02",
      "latest_appointment_time": "16:00",
      "spoken": "9월 2일 수요일",
      "weekday": "수"
     }
    ],
    "latest_time_comparison": {
     "dates": [
      "2026-09-01",
      "2026-09-02"
     ],
     "scope": "returned_dates",
     "time": "16:00"
    },
    "presentation": {
     "has_more": false,
     "instruction": "available_dates에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "date",
     "remaining_count": 0,
     "returned": 3,
     "total_available": 3
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 날짜를 선택한 뒤 그 날짜의 정확한 시간을 search_schedules로 조회하세요. 아직 stage_booking을 호출하지 마세요.",
      "tool": "search_schedules",
      "when": "after_customer_selects_date"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요. latest_time_comparison은 반환된 후보 날짜와 적용된 시간 조건 안에서 마지막 예약 가능 시각을 비교한 값입니다. 시간 조건 밖의 그날 최댓값, 영업 종료시간, 미조회 날짜의 최댓값이 아닙니다. 가장 늦은 시각을 물으면 이 비교의 시각과 동률 날짜를 안내하세요. 시각 비교 요청만으로 예약 확인 질문을 하거나 stage_booking을 호출하지 말고 고객이 날짜·시간을 선택할 때까지 기다리세요."
    },
    "searched_range": {
     "end_date": "2026-09-02",
     "spoken": "8월 31일 월요일부터 9월 2일 수요일까지",
     "start_date": "2026-08-31"
    },
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-31",
    "2026-09-01",
    "2026-09-02"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-02",
    "operator_id": null,
    "start_date": "2026-08-31",
    "strictly_after": "2026-08-31 10:00",
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-02",
     "operator_id": null,
     "start_date": "2026-08-31",
     "strictly_after": "2026-08-31 10:00",
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_service": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 3,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-08-28",
    "date_spoken": "8월 28일 금요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 3,
     "total_available": 3
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 11:00",
      "advisors": [
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 11시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 15:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오후 3시",
      "weekday": "금"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-28 09:00",
    "2026-08-28 11:00",
    "2026-08-28 15:00"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_earliest": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 1,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-08-28",
    "date_spoken": "8월 28일 금요일",
    "earliest_only_truncated": {
     "instruction": "같은 조건에 더 늦은 시각이 남아 있습니다. 이 슬롯을 그 날의 유일한 선택지로 말하지 마세요. 고객이 다른 시각을 물으면 strictly_after로 다시 조회하세요.",
     "omitted_later_slots": 2,
     "returned": 1
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시",
      "weekday": "금"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": null,
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": {
    "__dataclass__": "EarliestOffer",
    "datetime": "2026-08-28 09:00",
    "service_id": 3590,
    "operator_id": null
   },
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_empty_time_filtered": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 1
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 4720,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "availability": "none_in_searched_range",
    "date": "2026-08-29",
    "date_spoken": "8월 29일 토요일",
    "request_match": {
     "alternative_slot_count": 0,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "can_search_later_dates": true,
     "confirmation_allowed": false,
     "handoff_available": false,
     "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 아닙니다. 다음 순서로 하나를 고르세요. 1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하세요. handoff_available=false이면 시간을 유지해 다른 날짜를 찾을지 물으세요. 2. 그런 명시적 제한 없이 상담사만 바꿨다면 '같은 날 다른 시간도 괜찮으세요?'라고 물으세요. 날짜 변경을 먼저 제안하지 마세요. 3. 그 외에는 고객이 밝힌 선호를 따르고, 불분명하면 조정 가능한 조건 하나를 질문하세요. 답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. 다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. ",
     "wait_for_customer": true
    },
    "search_horizon_reached": false,
    "searched_range": {
     "end_date": "2026-08-29",
     "spoken": "8월 29일 토요일",
     "start_date": "2026-08-29"
    },
    "searched_time_window": {
     "from": "14:00",
     "instruction": "이 결과는 위 시각 범위만 확인했습니다. 범위 밖 시각을 고객이 물으면 가능·불가를 말하기 전에 그 시각으로 search_schedules를 다시 호출하세요.",
     "to": "23:59"
    },
    "slots": [],
    "status": "ok",
    "time_from": "14:00",
    "time_to": null
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [
    "2026-08-29~2026-08-29"
   ],
   "_last_presented": [],
   "_last_search_conditions": {
    "service_id": 4720,
    "end_date": "2026-08-29",
    "operator_id": null,
    "start_date": "2026-08-29",
    "strictly_after": null,
    "target_time": null,
    "time_from": "14:00",
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 0,
    "filters": {
     "service_id": 4720,
     "time_from": "14:00"
    },
    "horizon_reached": false,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-08-29",
     "2026-08-29"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 4720,
     "end_date": "2026-08-29",
     "operator_id": null,
     "start_date": "2026-08-29",
     "strictly_after": null,
     "target_time": null,
     "time_from": "14:00",
     "time_to": null
    }
   }
  }
 },
 "one_day_empty_whole_day": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 0
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "availability": "none_in_searched_range",
    "date": "2026-09-09",
    "date_spoken": "9월 9일 수요일",
    "request_match": {
     "alternative_slot_count": 0,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "can_search_later_dates": true,
     "confirmation_allowed": false,
     "handoff_available": false,
     "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 아닙니다. 다음 순서로 하나를 고르세요. 1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하세요. handoff_available=false이면 시간을 유지해 다른 날짜를 찾을지 물으세요. 2. 시간 조건 없이 조회한 날짜 전체가 비었으므로 같은 날 다른 시간·시간대를 제안하거나 묻지 말고, 다른 날짜를 찾을지 물으세요. other_advisors_available=true이면 같은 날 다른 상담사는 가능하다고 안내하고 그렇게 찾을지도 함께 물으세요. 답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. 다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. ",
     "wait_for_customer": true
    },
    "search_horizon_reached": false,
    "searched_range": {
     "end_date": "2026-09-09",
     "spoken": "9월 9일 수요일",
     "start_date": "2026-09-09"
    },
    "slots": [],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [
    "2026-09-09~2026-09-09"
   ],
   "_last_presented": [],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-09-09",
    "operator_id": null,
    "start_date": "2026-09-09",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 0,
    "filters": {
     "service_id": 3590
    },
    "horizon_reached": false,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-09-09",
     "2026-09-09"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-09-09",
     "operator_id": null,
     "start_date": "2026-09-09",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_no_service": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 4,
      "service_id": null,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "available_services": [
     {
      "id": 3590,
      "name": "휴대폰 점검"
     },
     {
      "id": 4720,
      "name": "노트북 상담"
     }
    ],
    "date": "2026-08-28",
    "date_spoken": "8월 28일 금요일",
    "presentation": {
     "has_more": true,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 1,
     "returned": 3,
     "total_available": 4
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "available_services": [
       {
        "id": 3590,
        "name": "휴대폰 점검"
       }
      ],
      "datetime": "2026-08-28 09:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시",
      "weekday": "금"
     },
     {
      "available_services": [
       {
        "id": 4720,
        "name": "노트북 상담"
       }
      ],
      "datetime": "2026-08-28 09:30",
      "advisors": [
       {
        "id": 806,
        "name": "이테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시 30분",
      "weekday": "금"
     },
     {
      "available_services": [
       {
        "id": 3590,
        "name": "휴대폰 점검"
       },
       {
        "id": 4720,
        "name": "노트북 상담"
       }
      ],
      "datetime": "2026-08-28 11:00",
      "advisors": [
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 11시",
      "weekday": "금"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-28 09:00",
    "2026-08-28 09:30",
    "2026-08-28 11:00"
   ],
   "_last_search_conditions": {
    "service_id": null,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": null,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_operator_miss_other_advisors": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 1
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 4720,
      "operator_id": 807
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "availability": "none_in_searched_range",
    "date": "2026-08-29",
    "date_spoken": "8월 29일 토요일",
    "other_advisors_available": true,
    "request_match": {
     "alternative_slot_count": 1,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "can_search_later_dates": true,
     "confirmation_allowed": false,
     "handoff_available": false,
     "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 아닙니다. 다음 순서로 하나를 고르세요. 1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하세요. handoff_available=false이면 시간을 유지해 다른 날짜를 찾을지 물으세요. 2. 시간 조건 없이 조회한 날짜 전체가 비었으므로 같은 날 다른 시간·시간대를 제안하거나 묻지 말고, 다른 날짜를 찾을지 물으세요. other_advisors_available=true이면 같은 날 다른 상담사는 가능하다고 안내하고 그렇게 찾을지도 함께 물으세요. 답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. 다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. ",
     "wait_for_customer": true
    },
    "search_horizon_reached": false,
    "searched_range": {
     "end_date": "2026-08-29",
     "spoken": "8월 29일 토요일",
     "start_date": "2026-08-29"
    },
    "slots": [],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [
    "2026-08-29~2026-08-29"
   ],
   "_last_presented": [],
   "_last_search_conditions": {
    "service_id": 4720,
    "end_date": "2026-08-29",
    "operator_id": 807,
    "start_date": "2026-08-29",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 1,
    "filters": {
     "service_id": 4720,
     "operator_id": 807
    },
    "horizon_reached": false,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-08-29",
     "2026-08-29"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 4720,
     "end_date": "2026-08-29",
     "operator_id": 807,
     "start_date": "2026-08-29",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_select_operator": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 3,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-08-28",
    "date_spoken": "8월 28일 금요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 3,
     "total_available": 3
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 11:00",
      "advisors": [
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 11시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 15:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오후 3시",
      "weekday": "금"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-28 09:00",
    "2026-08-28 11:00",
    "2026-08-28 15:00"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_single_slot_autostage": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 1
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 1,
      "service_id": 4720,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-08-29",
    "date_spoken": "8월 29일 토요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 1,
     "total_available": 1
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "instruction": "지금 같은 응답에서 stage_args 그대로 stage_booking을 호출하세요. 호출 전 발화·새 동의 질문·턴 종료는 금지합니다. 성공 후 한 발화로 답하세요. 고객이 가능 여부를 물었다면 '네, 가능합니다'처럼 그 답을 먼저 짧게 말하고, 이어서 반환된 confirmation의 전체 서비스명·날짜·시간과 제공된 상담사명으로 예약 신청 여부를 한 번 묻고 고객의 새 명시적 동의를 기다리세요. stage_booking은 예약 신청이 아니므로 commit_booking을 연속 호출하지 마세요.",
     "required_tool": "stage_booking",
     "stage_args": {
      "datetime": "2026-08-29 13:00",
      "service_id": 4720
     }
    },
    "searched_range": {
     "end_date": "2026-08-29",
     "spoken": "8월 29일 토요일",
     "start_date": "2026-08-29"
    },
    "slots": [
     {
      "datetime": "2026-08-29 13:00",
      "advisors": [
       {
        "id": 806,
        "name": "이테스트"
       }
      ],
      "spoken": "8월 29일 토요일 오후 1시",
      "weekday": "토"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-29 13:00"
   ],
   "_last_search_conditions": {
    "service_id": 4720,
    "end_date": "2026-08-29",
    "operator_id": null,
    "start_date": "2026-08-29",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 4720,
     "end_date": "2026-08-29",
     "operator_id": null,
     "start_date": "2026-08-29",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_target_hit": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 1,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-08-28",
    "date_spoken": "8월 28일 금요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 1,
     "total_available": 1
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "instruction": "지금 같은 응답에서 stage_args 그대로 stage_booking을 호출하세요. 호출 전 발화·새 동의 질문·턴 종료는 금지합니다. 성공 후 한 발화로 답하세요. 고객이 가능 여부를 물었다면 '네, 가능합니다'처럼 그 답을 먼저 짧게 말하고, 이어서 반환된 confirmation의 전체 서비스명·날짜·시간과 제공된 상담사명으로 예약 신청 여부를 한 번 묻고 고객의 새 명시적 동의를 기다리세요. stage_booking은 예약 신청이 아니므로 commit_booking을 연속 호출하지 마세요.",
     "required_tool": "stage_booking",
     "stage_args": {
      "datetime": "2026-08-28 15:00",
      "service_id": 3590
     }
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "searched_time_window": {
     "from": "15:00",
     "instruction": "이 결과는 위 시각 범위만 확인했습니다. 범위 밖 시각을 고객이 물으면 가능·불가를 말하기 전에 그 시각으로 search_schedules를 다시 호출하세요.",
     "to": "15:00"
    },
    "slots": [
     {
      "datetime": "2026-08-28 15:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오후 3시",
      "weekday": "금"
     }
    ],
    "status": "ok",
    "target_time": "15:00"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-28 15:00"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": "15:00",
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": "15:00",
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_target_miss_nearby": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 0,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-08-28",
    "date_spoken": "8월 28일 금요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 2,
     "total_available": 2
    },
    "request_match": {
     "alternative_slot_count": 2,
     "matching_slot_count": 0
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "handoff_available": false,
     "instruction": "searched_range에서 요청한 정확한 시각에는 자리가 없다고 안내하고, slots에 있는 그날의 가까운 시간만 가능한 대안으로 제시하세요. 고객이 이미 말한 시간이 slots에 있으면 그 시간을 우선 짚으세요. 다른 날짜를 먼저 제안하지 마세요. 고객이 그 시간만 가능하다고 명시했다면 같은 날 대안을 권하지 말고, handoff_available=true이면 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하고 아니면 다른 날짜를 찾을지 물으세요. 고객이 대안을 선택하기 전에는 stage_booking이나 예약 신청 확인 질문을 하지 말고 답변을 기다리세요.",
     "wait_for_customer": true
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "searched_time_window": {
     "from": "10:00",
     "instruction": "이 결과는 위 시각 범위만 확인했습니다. 범위 밖 시각을 고객이 물으면 가능·불가를 말하기 전에 그 시각으로 search_schedules를 다시 호출하세요.",
     "to": "10:00"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 11:00",
      "advisors": [
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 11시",
      "weekday": "금"
     }
    ],
    "status": "ok",
    "target_time": "10:00"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-28 09:00",
    "2026-08-28 11:00"
   ],
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": "10:00",
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": {
    "__dataclass__": "NoMatchingSlotBlocker",
    "alternative_slot_count": 2,
    "filters": {
     "service_id": 3590,
     "target_time": "10:00"
    },
    "horizon_reached": false,
    "kind": "no_matching_slot",
    "range_complete": true,
    "searched_range": [
     "2026-08-28",
     "2026-08-28"
    ]
   },
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": "10:00",
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "one_day_today_floor": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 3
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 2,
      "service_id": null,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   }
  ],
  "outputs": [
   {
    "available_services": [
     {
      "id": 3590,
      "name": "휴대폰 점검"
     },
     {
      "id": 4720,
      "name": "노트북 상담"
     }
    ],
    "date": "2026-08-27",
    "date_spoken": "8월 27일 목요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 2,
     "total_available": 2
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-27",
     "spoken": "8월 27일 목요일",
     "start_date": "2026-08-27"
    },
    "slots": [
     {
      "available_services": [
       {
        "id": 3590,
        "name": "휴대폰 점검"
       },
       {
        "id": 4720,
        "name": "노트북 상담"
       }
      ],
      "datetime": "2026-08-27 10:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       },
       {
        "id": 806,
        "name": "이테스트"
       }
      ],
      "spoken": "8월 27일 목요일 오전 10시",
      "weekday": "목"
     },
     {
      "available_services": [
       {
        "id": 3590,
        "name": "휴대폰 점검"
       },
       {
        "id": 4720,
        "name": "노트북 상담"
       }
      ],
      "datetime": "2026-08-27 14:00",
      "advisors": [
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "8월 27일 목요일 오후 2시",
      "weekday": "목"
     }
    ],
    "status": "ok"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": [
    "2026-08-27 10:00",
    "2026-08-27 14:00"
   ],
   "_last_search_conditions": {
    "service_id": null,
    "end_date": "2026-08-27",
    "operator_id": null,
    "start_date": "2026-08-27",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": null,
     "end_date": "2026-08-27",
     "operator_id": null,
     "start_date": "2026-08-27",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "stage_select_operator_choice": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 3
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "datetime": "2026-08-27 10:00",
      "service_id": 4720,
      "operator_assigned": true,
      "operator_id": 806
     },
     "name": "BOOKING_CREATE",
     "status": "STAGED"
    }
   }
  ],
  "outputs": [
   {
    "available_operators": [
     {
      "id": 806,
      "name": "이테스트"
     }
    ],
    "confirmation": {
     "date": "2026-08-27",
     "datetime": "2026-08-27 10:00",
     "service_name": "노트북 상담",
     "is_earliest": false,
     "language": "ko-KR",
     "operator_name": "이테스트",
     "time": "10:00",
     "weekday_iso": 4
    },
    "operator_assigned": true,
    "required_next_action": "ask_booking_confirmation",
    "staged": {
     "datetime": "2026-08-27 10:00",
     "service_id": 4720,
     "service_name": "노트북 상담",
     "operator_id": 806,
     "operator_name": "이테스트"
    },
    "status": "staged"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": null,
   "_last_presented": null,
   "_last_search_conditions": {
    "service_id": 4720
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 1,
   "_schedule_blocker": null,
   "_searched_in_task": false,
   "_staged": {
    "__dataclass__": "StagedBooking",
    "datetime": "2026-08-27 10:00",
    "service_id": 4720,
    "service_name": "노트북 상담",
    "operator_id": 806,
    "operator_name": "이테스트"
   },
   "pending": {
    "__dataclass__": "DynamicBookingPending",
    "datetime": "2026-08-27 10:00",
    "service_id": 4720,
    "service_name": "노트북 상담",
    "operator_id": 806,
    "operator_name": "이테스트"
   },
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 4720
    }
   }
  }
 },
 "stage_success_after_search": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   },
   {
    "agent_event": {
     "details": {
      "count": 3,
      "service_id": 3590,
      "operator_id": null
     },
     "name": "BOOKING_CREATE",
     "status": "SLOTS_SEARCHED"
    }
   },
   {
    "agent_event": {
     "details": {
      "datetime": "2026-08-28 09:00",
      "service_id": 3590,
      "operator_assigned": true,
      "operator_id": 805
     },
     "name": "BOOKING_CREATE",
     "status": "STAGED"
    }
   }
  ],
  "outputs": [
   {
    "date": "2026-08-28",
    "date_spoken": "8월 28일 금요일",
    "presentation": {
     "has_more": false,
     "instruction": "slots에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 물으세요.",
     "max_options": 3,
     "option_kind": "datetime",
     "remaining_count": 0,
     "returned": 3,
     "total_available": 3
    },
    "requested_range_complete": true,
    "required_next_action": {
     "confirmation_allowed": false,
     "deferred_action": {
      "instruction": "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 호출하세요. 선택 전에는 호출하지 마세요.",
      "tool": "stage_booking",
      "when": "after_customer_selects_exact_slot_and_service"
     },
     "instruction": "search_schedules 결과만으로 확인 질문을 만들지 마세요. stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
    },
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [
     {
      "datetime": "2026-08-28 09:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 9시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 11:00",
      "advisors": [
       {
        "id": 807,
        "name": "박테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오전 11시",
      "weekday": "금"
     },
     {
      "datetime": "2026-08-28 15:00",
      "advisors": [
       {
        "id": 805,
        "name": "김테스트"
       }
      ],
      "spoken": "8월 28일 금요일 오후 3시",
      "weekday": "금"
     }
    ],
    "status": "ok"
   },
   {
    "confirmation": {
     "date": "2026-08-28",
     "datetime": "2026-08-28 09:00",
     "service_name": "휴대폰 점검",
     "is_earliest": false,
     "language": "ko-KR",
     "time": "09:00",
     "weekday_iso": 5
    },
    "required_next_action": "ask_booking_confirmation",
    "staged": {
     "datetime": "2026-08-28 09:00",
     "service_id": 3590,
     "service_name": "휴대폰 점검",
     "operator_id": 805,
     "operator_name": "김테스트"
    },
    "status": "staged"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": [],
   "_last_presented": null,
   "_last_search_conditions": {
    "service_id": 3590,
    "end_date": "2026-08-28",
    "operator_id": null,
    "start_date": "2026-08-28",
    "strictly_after": null,
    "target_time": null,
    "time_from": null,
    "time_to": null
   },
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": false,
   "_pending_empty_search_user_message_id": null,
   "_rejected_candidates": null,
   "_request_revision": 2,
   "_schedule_blocker": null,
   "_searched_in_task": true,
   "_staged": {
    "__dataclass__": "StagedBooking",
    "datetime": "2026-08-28 09:00",
    "service_id": 3590,
    "service_name": "휴대폰 점검",
    "operator_id": 805,
    "operator_name": "김테스트"
   },
   "pending": {
    "__dataclass__": "DynamicBookingPending",
    "datetime": "2026-08-28 09:00",
    "service_id": 3590,
    "service_name": "휴대폰 점검",
    "operator_id": 805,
    "operator_name": "김테스트"
   },
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {
     "service_id": 3590,
     "end_date": "2026-08-28",
     "operator_id": null,
     "start_date": "2026-08-28",
     "strictly_after": null,
     "target_time": null,
     "time_from": null,
     "time_to": null
    }
   }
  }
 },
 "stage_unavailable": {
  "events": [
   {
    "agent_event": {
     "details": {
      "api": "GET_SITE_AVAILABLE_SCHEDULES",
      "result_count": 4
     },
     "name": "API_RESULT",
     "status": "SUCCESS"
    }
   }
  ],
  "outputs": [
   {
    "requested": "8월 28일 금요일 오전 10시",
    "required_next_action": {
     "can_search_later_dates": true,
     "confirmation_allowed": false,
     "handoff_available": false,
     "instruction": "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. 예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. 같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 답변을 기다리세요. target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 아닙니다. 다음 순서로 하나를 고르세요. 1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')를 호출하세요. handoff_available=false이면 시간을 유지해 다른 날짜를 찾을지 물으세요. 2. 그런 명시적 제한 없이 상담사만 바꿨다면 '같은 날 다른 시간도 괜찮으세요?'라고 물으세요. 날짜 변경을 먼저 제안하지 마세요. 3. 그 외에는 고객이 밝힌 선호를 따르고, 불분명하면 조정 가능한 조건 하나를 질문하세요. 답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. 다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. ",
     "wait_for_customer": true
    },
    "search_horizon_reached": false,
    "searched_range": {
     "end_date": "2026-08-28",
     "spoken": "8월 28일 금요일",
     "start_date": "2026-08-28"
    },
    "slots": [],
    "status": "unavailable",
    "target_time": "10:00"
   }
  ],
  "state": {
   "_declined_turn_id": null,
   "_empty_searched_ranges": null,
   "_last_presented": null,
   "_last_search_conditions": null,
   "_offer_pending": null,
   "_pending_earliest_offer": null,
   "_pending_empty_search_set": true,
   "_pending_empty_search_user_message_id": "u1",
   "_rejected_candidates": null,
   "_request_revision": 0,
   "_schedule_blocker": null,
   "_searched_in_task": false,
   "_staged": null,
   "pending": null,
   "resume": {
    "__dataclass__": "DynamicBookingResumeContext",
    "declined_alternatives": 0,
    "advisor_alternative_presented": false,
    "excluded_dates": [],
    "handoff_declined_evidence": null,
    "handoff_offer_delivered": true,
    "last_exit_reason": null,
    "last_handled_user_message_id": null,
    "paused": false,
    "rejected_candidates": [],
    "search_conditions": {}
   }
  }
 }
}
"""
