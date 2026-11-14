"""Golden LiveKit tool schemas of DynamicBookingTask (pre-refactor capture).

Regenerate only for an intentional tool-schema change."""

GOLDEN_JSON = r"""
{
 "answer_handoff_offer": {
  "legacy": {
   "type": "function",
   "function": {
    "name": "answer_handoff_offer",
    "description": "Report the caller's answer to the staff-help question you just asked.\n\naccept: an explicit yes to the offered connection or message (\"네\n남겨주세요\", \"연결해 주세요\"); restating a condition is not consent.\ndecline: refuses the help (\"연결은 싫고 예약만 할게요\", \"괜찮아요\"), also\ntogether with new conditions; then search those conditions. other: new\nbooking conditions only, or anything else; continue with it. Output\nnothing after accept; the parent handles it.",
    "parameters": {
     "properties": {
      "reply": {
       "enum": [
        "accept",
        "decline",
        "other"
       ],
       "title": "Reply",
       "type": "string"
      }
     },
     "required": [
      "reply"
     ],
     "title": "AnswerHandoffOfferArgs",
     "type": "object"
    }
   }
  },
  "strict": {
   "type": "function",
   "function": {
    "name": "answer_handoff_offer",
    "strict": true,
    "description": "Report the caller's answer to the staff-help question you just asked.\n\naccept: an explicit yes to the offered connection or message (\"네\n남겨주세요\", \"연결해 주세요\"); restating a condition is not consent.\ndecline: refuses the help (\"연결은 싫고 예약만 할게요\", \"괜찮아요\"), also\ntogether with new conditions; then search those conditions. other: new\nbooking conditions only, or anything else; continue with it. Output\nnothing after accept; the parent handles it.",
    "parameters": {
     "properties": {
      "reply": {
       "enum": [
        "accept",
        "decline",
        "other"
       ],
       "type": "string"
      }
     },
     "required": [
      "reply"
     ],
     "type": "object",
     "additionalProperties": false
    }
   }
  }
 },
 "commit_booking": {
  "legacy": {
   "type": "function",
   "function": {
    "name": "commit_booking",
    "description": "Commit only the candidate the caller just explicitly accepted.\n\nCopy ``confirmed_datetime`` from that confirmation. \"네?\", \"예?\" and\n\"뭐라고요?\" request repetition and are not consent. On any error, follow\nits recovery action; never force a match by retrying commit. Success is\nterminal for this task: output nothing, because the parent announces the\nresult and ends the call.",
    "parameters": {
     "properties": {
      "confirmed_datetime": {
       "title": "Confirmed Datetime",
       "type": "string"
      }
     },
     "required": [
      "confirmed_datetime"
     ],
     "title": "CommitBookingArgs",
     "type": "object"
    }
   }
  },
  "strict": {
   "type": "function",
   "function": {
    "name": "commit_booking",
    "strict": true,
    "description": "Commit only the candidate the caller just explicitly accepted.\n\nCopy ``confirmed_datetime`` from that confirmation. \"네?\", \"예?\" and\n\"뭐라고요?\" request repetition and are not consent. On any error, follow\nits recovery action; never force a match by retrying commit. Success is\nterminal for this task: output nothing, because the parent announces the\nresult and ends the call.",
    "parameters": {
     "properties": {
      "confirmed_datetime": {
       "type": "string"
      }
     },
     "required": [
      "confirmed_datetime"
     ],
     "type": "object",
     "additionalProperties": false
    }
   }
  }
 },
 "exit_task": {
  "legacy": {
   "type": "function",
   "function": {
    "name": "exit_task",
    "description": "Leave create-booking only for a clear stop, staff request, or new intent.\n\nFor a whole-booking stop, call cancel; do not merely acknowledge it. This\ntool speaks the stop question; only a fresh explicit yes permits\ncancel_confirmed=true, false continues booking. Candidate refusal uses\nreject_candidate, never cancel. staff_connection: the caller explicitly\nasks for a person, or reports a device safety issue (battery swelling,\noverheating, smoke, burning smell). unrelated: existing-appointment\nrequests; for unrelated/other choose route_target booking_inquiry,\nbooking_modify, booking_cancel, end_call, other. Never use transfer;\nstaff_connection omits route_target. Answer service center information here\nwithout exiting. Unbookable requests use report_booking_obstacle and\nreplies to a handoff question use answer_handoff_offer, never this tool.\nGround detail/time_condition in caller words. After completion output\nnothing; the parent handles it.",
    "parameters": {
     "$defs": {
      "NullableBookingRouteTarget": {
       "anyOf": [
        {
         "enum": [
          "booking_inquiry",
          "booking_modify",
          "booking_cancel",
          "end_call",
          "transfer",
          "change_service",
          "change_advisor",
          "no_slot_memo",
          "staff_connection",
          "service_not_offered",
          "cancel",
          "unrelated",
          "other"
         ],
         "type": "string"
        },
        {
         "type": "null"
        }
       ]
      },
      "NullableToolBool": {
       "anyOf": [
        {
         "type": "boolean"
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "properties": {
      "reason_code": {
       "default": "other",
       "enum": [
        "no_slot_memo",
        "staff_connection",
        "service_not_offered",
        "cancel",
        "unrelated",
        "other"
       ],
       "title": "Reason Code",
       "type": "string"
      },
      "detail": {
       "default": "",
       "title": "Detail",
       "type": "string"
      },
      "time_condition": {
       "anyOf": [
        {
         "type": "string"
        },
        {
         "type": "null"
        }
       ],
       "default": null,
       "title": "Time Condition"
      },
      "route_target": {
       "$ref": "#/$defs/NullableBookingRouteTarget",
       "default": null
      },
      "cancel_confirmed": {
       "$ref": "#/$defs/NullableToolBool",
       "default": null
      }
     },
     "title": "ExitTaskArgs",
     "type": "object"
    }
   }
  },
  "strict": {
   "type": "function",
   "function": {
    "name": "exit_task",
    "strict": true,
    "description": "Leave create-booking only for a clear stop, staff request, or new intent.\n\nFor a whole-booking stop, call cancel; do not merely acknowledge it. This\ntool speaks the stop question; only a fresh explicit yes permits\ncancel_confirmed=true, false continues booking. Candidate refusal uses\nreject_candidate, never cancel. staff_connection: the caller explicitly\nasks for a person, or reports a device safety issue (battery swelling,\noverheating, smoke, burning smell). unrelated: existing-appointment\nrequests; for unrelated/other choose route_target booking_inquiry,\nbooking_modify, booking_cancel, end_call, other. Never use transfer;\nstaff_connection omits route_target. Answer service center information here\nwithout exiting. Unbookable requests use report_booking_obstacle and\nreplies to a handoff question use answer_handoff_offer, never this tool.\nGround detail/time_condition in caller words. After completion output\nnothing; the parent handles it.",
    "parameters": {
     "$defs": {
      "NullableBookingRouteTarget": {
       "anyOf": [
        {
         "enum": [
          "booking_inquiry",
          "booking_modify",
          "booking_cancel",
          "end_call",
          "transfer",
          "change_service",
          "change_advisor",
          "no_slot_memo",
          "staff_connection",
          "service_not_offered",
          "cancel",
          "unrelated",
          "other",
          null
         ],
         "type": [
          "string",
          "null"
         ]
        },
        {
         "type": "null"
        }
       ]
      },
      "NullableToolBool": {
       "anyOf": [
        {
         "type": [
          "boolean",
          "null"
         ]
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "properties": {
      "reason_code": {
       "enum": [
        "no_slot_memo",
        "staff_connection",
        "service_not_offered",
        "cancel",
        "unrelated",
        "other",
        null
       ],
       "type": [
        "string",
        "null"
       ]
      },
      "detail": {
       "type": [
        "string",
        "null"
       ]
      },
      "time_condition": {
       "type": [
        "string",
        "null"
       ]
      },
      "route_target": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableBookingRouteTarget"
        },
        {
         "type": "null"
        }
       ]
      },
      "cancel_confirmed": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolBool"
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "type": "object",
     "additionalProperties": false,
     "required": [
      "reason_code",
      "detail",
      "time_condition",
      "route_target",
      "cancel_confirmed"
     ]
    }
   }
  }
 },
 "reconsider_candidate": {
  "legacy": {
   "type": "function",
   "function": {
    "name": "reconsider_candidate",
    "description": "Reopen one previously rejected slot the caller explicitly reselected in a\nlater turn, then search or stage it. Never use it to fill empty results or\nto reopen a whole rejected day.",
    "parameters": {
     "properties": {
      "datetime": {
       "title": "Datetime",
       "type": "string"
      }
     },
     "required": [
      "datetime"
     ],
     "title": "ReconsiderCandidateArgs",
     "type": "object"
    }
   }
  },
  "strict": {
   "type": "function",
   "function": {
    "name": "reconsider_candidate",
    "strict": true,
    "description": "Reopen one previously rejected slot the caller explicitly reselected in a\nlater turn, then search or stage it. Never use it to fill empty results or\nto reopen a whole rejected day.",
    "parameters": {
     "properties": {
      "datetime": {
       "type": "string"
      }
     },
     "required": [
      "datetime"
     ],
     "type": "object",
     "additionalProperties": false
    }
   }
  }
 },
 "record_caller_evidence": {
  "legacy": {
   "type": "function",
   "function": {
    "name": "record_caller_evidence",
    "description": "Record the caller's own words that justify a staff offer or a refusal.\n\nCall before the next search, only when the caller actually said it. Copy one\nshort phrase verbatim from a recent caller turn; never assistant text, a\nsummary, a service name, or a third-party quote.\nsafety_issue: the caller actually described a device safety issue\n(battery swelling, overheating, smoke, burning); an ordinary fault such as a\ncracked screen is NOT evidence. safety_issue_withdrawn: the caller denies or\nretracts that safety issue. handoff_refusal: an\nexplicit refusal of staff/memo help, even before any offer.",
    "parameters": {
     "properties": {
      "kind": {
       "enum": [
        "safety_issue",
        "safety_issue_withdrawn",
        "handoff_refusal"
       ],
       "title": "Kind",
       "type": "string"
      },
      "quote": {
       "title": "Quote",
       "type": "string"
      }
     },
     "required": [
      "kind",
      "quote"
     ],
     "title": "RecordCallerEvidenceArgs",
     "type": "object"
    }
   }
  },
  "strict": {
   "type": "function",
   "function": {
    "name": "record_caller_evidence",
    "strict": true,
    "description": "Record the caller's own words that justify a staff offer or a refusal.\n\nCall before the next search, only when the caller actually said it. Copy one\nshort phrase verbatim from a recent caller turn; never assistant text, a\nsummary, a service name, or a third-party quote.\nsafety_issue: the caller actually described a device safety issue\n(battery swelling, overheating, smoke, burning); an ordinary fault such as a\ncracked screen is NOT evidence. safety_issue_withdrawn: the caller denies or\nretracts that safety issue. handoff_refusal: an\nexplicit refusal of staff/memo help, even before any offer.",
    "parameters": {
     "properties": {
      "kind": {
       "enum": [
        "safety_issue",
        "safety_issue_withdrawn",
        "handoff_refusal"
       ],
       "type": "string"
      },
      "quote": {
       "type": "string"
      }
     },
     "required": [
      "kind",
      "quote"
     ],
     "type": "object",
     "additionalProperties": false
    }
   }
  }
 },
 "reject_candidate": {
  "legacy": {
   "type": "function",
   "function": {
    "name": "reject_candidate",
    "description": "Decline the pending candidate without stopping the booking task.\n\nUse slot for a simple no or a rejected time; date only when the caller\nrejects the whole day. This clears consent but retains service and time\npreferences. Then search with those preferences; use strictly_after only\nfor a later/next slot. Never use exit_task(cancel) for a candidate refusal.\nA returned handoff_offer replaces the search: ask it instead.",
    "parameters": {
     "properties": {
      "scope": {
       "default": "slot",
       "enum": [
        "slot",
        "date"
       ],
       "title": "Scope",
       "type": "string"
      }
     },
     "title": "RejectCandidateArgs",
     "type": "object"
    }
   }
  },
  "strict": {
   "type": "function",
   "function": {
    "name": "reject_candidate",
    "strict": true,
    "description": "Decline the pending candidate without stopping the booking task.\n\nUse slot for a simple no or a rejected time; date only when the caller\nrejects the whole day. This clears consent but retains service and time\npreferences. Then search with those preferences; use strictly_after only\nfor a later/next slot. Never use exit_task(cancel) for a candidate refusal.\nA returned handoff_offer replaces the search: ask it instead.",
    "parameters": {
     "properties": {
      "scope": {
       "enum": [
        "slot",
        "date",
        null
       ],
       "type": [
        "string",
        "null"
       ]
      }
     },
     "type": "object",
     "additionalProperties": false,
     "required": [
      "scope"
     ]
    }
   }
  }
 },
 "report_booking_obstacle": {
  "legacy": {
   "type": "function",
   "function": {
    "name": "report_booking_obstacle",
    "description": "Report that phone booking cannot meet the caller's current request.\n\nservice/advisor: the caller clearly wants a service or advisor (subject\nadvisor) that no injected list item matches — a first request, a refusal of\nlisted items, or restating the original (\"데이터 복구로 해야 돼\", \"저번에\n데이터 복구 받았는데 이걸로 해줘요\"). Pass the caller's own name for it as\nrequested_label. Call this instead of saying it cannot be booked; do not\noffer unrelated items first. Mentioning a past inspection is not a records\nrequest. If the meaning is unclear, ask instead. schedule: after a search\nwith no exact match, the caller says that date/time/advisor cannot change\n(\"그날 그 시간밖에 안 돼요\", \"그 상담사님한테 받아야 해요\").\nhandoff_offer: ask it as instructed and wait. handoff_suppressed: say only\nthat it cannot be booked by phone and continue booking.\nadvisor: pass service_id when the service is known.\ncaller_fixed_advisor=true only when the caller explicitly rules out every\nother advisor (\"그 상담사님 아니면 안 돼요\", \"다른 상담사는 싫어요\"); naming,\nrequesting or preferring an advisor is false. alternative_available: offer\nthe returned verified alternative once, in one question, with no times; if\nthe caller refuses it or keeps the original advisor, call this again.\nservice_required: explain only the phone booking scope limitation,\nthen ask the service without implying the requested advisor is bookable;\ncall this again with service_id after the answer.\nadvisor_confirmation_required: a listed advisor has a close name;\nask whether the caller means them, and\ncall this again only if not.",
    "parameters": {
     "$defs": {
      "NullableToolInt": {
       "anyOf": [
        {
         "type": "integer"
        },
        {
         "type": "null"
        }
       ]
      },
      "NullableToolStr": {
       "anyOf": [
        {
         "type": "string"
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "properties": {
      "subject": {
       "enum": [
        "service",
        "advisor",
        "schedule"
       ],
       "title": "Subject",
       "type": "string"
      },
      "requested_label": {
       "$ref": "#/$defs/NullableToolStr",
       "default": null
      },
      "service_id": {
       "$ref": "#/$defs/NullableToolInt",
       "default": null
      },
      "caller_fixed_advisor": {
       "default": false,
       "title": "Caller Fixed Advisor",
       "type": "boolean"
      }
     },
     "required": [
      "subject"
     ],
     "title": "ReportBookingObstacleArgs",
     "type": "object"
    }
   }
  },
  "strict": {
   "type": "function",
   "function": {
    "name": "report_booking_obstacle",
    "strict": true,
    "description": "Report that phone booking cannot meet the caller's current request.\n\nservice/advisor: the caller clearly wants a service or advisor (subject\nadvisor) that no injected list item matches — a first request, a refusal of\nlisted items, or restating the original (\"데이터 복구로 해야 돼\", \"저번에\n데이터 복구 받았는데 이걸로 해줘요\"). Pass the caller's own name for it as\nrequested_label. Call this instead of saying it cannot be booked; do not\noffer unrelated items first. Mentioning a past inspection is not a records\nrequest. If the meaning is unclear, ask instead. schedule: after a search\nwith no exact match, the caller says that date/time/advisor cannot change\n(\"그날 그 시간밖에 안 돼요\", \"그 상담사님한테 받아야 해요\").\nhandoff_offer: ask it as instructed and wait. handoff_suppressed: say only\nthat it cannot be booked by phone and continue booking.\nadvisor: pass service_id when the service is known.\ncaller_fixed_advisor=true only when the caller explicitly rules out every\nother advisor (\"그 상담사님 아니면 안 돼요\", \"다른 상담사는 싫어요\"); naming,\nrequesting or preferring an advisor is false. alternative_available: offer\nthe returned verified alternative once, in one question, with no times; if\nthe caller refuses it or keeps the original advisor, call this again.\nservice_required: explain only the phone booking scope limitation,\nthen ask the service without implying the requested advisor is bookable;\ncall this again with service_id after the answer.\nadvisor_confirmation_required: a listed advisor has a close name;\nask whether the caller means them, and\ncall this again only if not.",
    "parameters": {
     "$defs": {
      "NullableToolInt": {
       "anyOf": [
        {
         "type": [
          "integer",
          "null"
         ]
        },
        {
         "type": "null"
        }
       ]
      },
      "NullableToolStr": {
       "anyOf": [
        {
         "type": [
          "string",
          "null"
         ]
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "properties": {
      "subject": {
       "enum": [
        "service",
        "advisor",
        "schedule"
       ],
       "type": "string"
      },
      "requested_label": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolStr"
        },
        {
         "type": "null"
        }
       ]
      },
      "service_id": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolInt"
        },
        {
         "type": "null"
        }
       ]
      },
      "caller_fixed_advisor": {
       "type": [
        "boolean",
        "null"
       ]
      }
     },
     "required": [
      "subject",
      "requested_label",
      "service_id",
      "caller_fixed_advisor"
     ],
     "type": "object",
     "additionalProperties": false
    }
   }
  }
 },
 "search_schedules": {
  "legacy": {
   "type": "function",
   "function": {
    "name": "search_schedules",
    "description": "Search grounded availability; pass every active condition.\n\nDates are date-only ``YYYY-MM-DD``. Omit unused args or use JSON null;\nnever invent IDs or times as placeholders. One day returns slots; multiple days\nreturn dates. In list mode, a one-day exact-time miss may return up to\nthree verified slots within three hours, nearest first. Time-filtered ranges\ncheck at most seven days. Empty applies only to searched_range and\nfilters: announce it and wait.\n\nservice_id and operator_id must come from the injected lists. Omit\nservice_id only when no service is selected and operator_id only\nwhen no advisor is requested; never use 0 as an unspecified ID.\nAvailable-service questions use service_id=null before asking for\nthe device issue or service. \"Other services\" clears that filter; preserve\nthe caller's date/time and requested advisor.\n\nstatus=error with code schedule_lookup_failed is a system failure to\nannounce. retry=silent means fix arguments and retry without speaking;\nretry=clarify means ask the caller before changing conflicting constraints.\n\nTimes use local HH:MM. No requested window means time_from/time_to=null,\nnever invent a range. \"오전 11시\" is target_time only; \"오전만, 가능하면 11시\"\nkeeps both. \"오전만+13시\" conflicts: ask before searching; never invent or\nchange bounds. Inclusive bounds also constrain nearby alternatives.\nA window alone uses target_time=null.\n오전/아침 00:00..11:59, 오후 12:00..23:59, 저녁 17:00..23:59.\nUse strictly_after only for an explicit next/later\nrequest (\"다음\", \"이후\"), never for an exact-time question. Consent to\n\"다른 시간\" removes target_time but does not mean later: do not add\nstrictly_after or a new lower time bound without that request.\n\nmode=list is the default. mode=earliest returns the earliest slot for an\navailability-only question. mode=earliest_for_booking is for booking intent\nwithout date/time, requires service_id, and follows its stage_booking\naction. Search results alone never authorize a booking-consent question.\n\nSpeak dates only via spoken/date_spoken/searched_range.spoken; never\nrecompute them. date_corrected means the search ran on the date the\ncaller named: present that date and do not mention the replaced one.\nPresent at most three choices; honor presentation.has_more.\nlatest_time_comparison covers only returned dates and filters. Without\nservice_id, name available_services and wait for a choice.\nA returned handoff_offer replaces the alternative question: ask it.",
    "parameters": {
     "$defs": {
      "NullableToolInt": {
       "anyOf": [
        {
         "type": "integer"
        },
        {
         "type": "null"
        }
       ]
      },
      "NullableToolStr": {
       "anyOf": [
        {
         "type": "string"
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "properties": {
      "start_date": {
       "title": "Start Date",
       "type": "string"
      },
      "end_date": {
       "title": "End Date",
       "type": "string"
      },
      "service_id": {
       "$ref": "#/$defs/NullableToolInt",
       "default": null
      },
      "operator_id": {
       "$ref": "#/$defs/NullableToolInt",
       "default": null
      },
      "time_from": {
       "$ref": "#/$defs/NullableToolStr",
       "default": null
      },
      "time_to": {
       "$ref": "#/$defs/NullableToolStr",
       "default": null
      },
      "target_time": {
       "$ref": "#/$defs/NullableToolStr",
       "default": null
      },
      "strictly_after": {
       "$ref": "#/$defs/NullableToolStr",
       "default": null
      },
      "mode": {
       "default": "list",
       "enum": [
        "list",
        "earliest",
        "earliest_for_booking"
       ],
       "title": "Mode",
       "type": "string"
      }
     },
     "required": [
      "start_date",
      "end_date"
     ],
     "title": "SearchSchedulesArgs",
     "type": "object"
    }
   }
  },
  "strict": {
   "type": "function",
   "function": {
    "name": "search_schedules",
    "strict": true,
    "description": "Search grounded availability; pass every active condition.\n\nDates are date-only ``YYYY-MM-DD``. Omit unused args or use JSON null;\nnever invent IDs or times as placeholders. One day returns slots; multiple days\nreturn dates. In list mode, a one-day exact-time miss may return up to\nthree verified slots within three hours, nearest first. Time-filtered ranges\ncheck at most seven days. Empty applies only to searched_range and\nfilters: announce it and wait.\n\nservice_id and operator_id must come from the injected lists. Omit\nservice_id only when no service is selected and operator_id only\nwhen no advisor is requested; never use 0 as an unspecified ID.\nAvailable-service questions use service_id=null before asking for\nthe device issue or service. \"Other services\" clears that filter; preserve\nthe caller's date/time and requested advisor.\n\nstatus=error with code schedule_lookup_failed is a system failure to\nannounce. retry=silent means fix arguments and retry without speaking;\nretry=clarify means ask the caller before changing conflicting constraints.\n\nTimes use local HH:MM. No requested window means time_from/time_to=null,\nnever invent a range. \"오전 11시\" is target_time only; \"오전만, 가능하면 11시\"\nkeeps both. \"오전만+13시\" conflicts: ask before searching; never invent or\nchange bounds. Inclusive bounds also constrain nearby alternatives.\nA window alone uses target_time=null.\n오전/아침 00:00..11:59, 오후 12:00..23:59, 저녁 17:00..23:59.\nUse strictly_after only for an explicit next/later\nrequest (\"다음\", \"이후\"), never for an exact-time question. Consent to\n\"다른 시간\" removes target_time but does not mean later: do not add\nstrictly_after or a new lower time bound without that request.\n\nmode=list is the default. mode=earliest returns the earliest slot for an\navailability-only question. mode=earliest_for_booking is for booking intent\nwithout date/time, requires service_id, and follows its stage_booking\naction. Search results alone never authorize a booking-consent question.\n\nSpeak dates only via spoken/date_spoken/searched_range.spoken; never\nrecompute them. date_corrected means the search ran on the date the\ncaller named: present that date and do not mention the replaced one.\nPresent at most three choices; honor presentation.has_more.\nlatest_time_comparison covers only returned dates and filters. Without\nservice_id, name available_services and wait for a choice.\nA returned handoff_offer replaces the alternative question: ask it.",
    "parameters": {
     "$defs": {
      "NullableToolInt": {
       "anyOf": [
        {
         "type": [
          "integer",
          "null"
         ]
        },
        {
         "type": "null"
        }
       ]
      },
      "NullableToolStr": {
       "anyOf": [
        {
         "type": [
          "string",
          "null"
         ]
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "properties": {
      "start_date": {
       "type": "string"
      },
      "end_date": {
       "type": "string"
      },
      "service_id": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolInt"
        },
        {
         "type": "null"
        }
       ]
      },
      "operator_id": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolInt"
        },
        {
         "type": "null"
        }
       ]
      },
      "time_from": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolStr"
        },
        {
         "type": "null"
        }
       ]
      },
      "time_to": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolStr"
        },
        {
         "type": "null"
        }
       ]
      },
      "target_time": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolStr"
        },
        {
         "type": "null"
        }
       ]
      },
      "strictly_after": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolStr"
        },
        {
         "type": "null"
        }
       ]
      },
      "mode": {
       "enum": [
        "list",
        "earliest",
        "earliest_for_booking",
        null
       ],
       "type": [
        "string",
        "null"
       ]
      }
     },
     "required": [
      "start_date",
      "end_date",
      "service_id",
      "operator_id",
      "time_from",
      "time_to",
      "target_time",
      "strictly_after",
      "mode"
     ],
     "type": "object",
     "additionalProperties": false
    }
   }
  }
 },
 "stage_booking": {
  "legacy": {
   "type": "function",
   "function": {
    "name": "stage_booking",
    "description": "Validate and stage one exact candidate before asking for consent.\n\n``datetime`` must be grounded and formatted ``YYYY-MM-DD HH:MM``. Pass\noperator_id when the caller named an advisor; otherwise omit it. On success,\nask one natural question using every value in ``confirmation``: full\nservice, date, time, and operator_name only when present. Preserve\nthe full service label, even repeated parent/child names and qualifiers.\nLocalize weekday_iso without recalculating. If is_earliest is true,\nsay \"earliest\"; otherwise do not. This is pending, not completed.\nWait for explicit agreement, then commit confirmation.datetime.",
    "parameters": {
     "$defs": {
      "NullableToolInt": {
       "anyOf": [
        {
         "type": "integer"
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "properties": {
      "service_id": {
       "title": "Service Id",
       "type": "integer"
      },
      "datetime": {
       "title": "Datetime",
       "type": "string"
      },
      "operator_id": {
       "$ref": "#/$defs/NullableToolInt",
       "default": null
      }
     },
     "required": [
      "service_id",
      "datetime"
     ],
     "title": "StageBookingArgs",
     "type": "object"
    }
   }
  },
  "strict": {
   "type": "function",
   "function": {
    "name": "stage_booking",
    "strict": true,
    "description": "Validate and stage one exact candidate before asking for consent.\n\n``datetime`` must be grounded and formatted ``YYYY-MM-DD HH:MM``. Pass\noperator_id when the caller named an advisor; otherwise omit it. On success,\nask one natural question using every value in ``confirmation``: full\nservice, date, time, and operator_name only when present. Preserve\nthe full service label, even repeated parent/child names and qualifiers.\nLocalize weekday_iso without recalculating. If is_earliest is true,\nsay \"earliest\"; otherwise do not. This is pending, not completed.\nWait for explicit agreement, then commit confirmation.datetime.",
    "parameters": {
     "$defs": {
      "NullableToolInt": {
       "anyOf": [
        {
         "type": [
          "integer",
          "null"
         ]
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "properties": {
      "service_id": {
       "type": "integer"
      },
      "datetime": {
       "type": "string"
      },
      "operator_id": {
       "anyOf": [
        {
         "$ref": "#/$defs/NullableToolInt"
        },
        {
         "type": "null"
        }
       ]
      }
     },
     "required": [
      "service_id",
      "datetime",
      "operator_id"
     ],
     "type": "object",
     "additionalProperties": false
    }
   }
  }
 }
}
"""
