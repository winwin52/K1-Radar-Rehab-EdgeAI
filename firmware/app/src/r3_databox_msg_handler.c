/**
 **************************************************************************************************
 * @brief   project config define.
 * @attention
 *
 * Copyright (C) 2025 POSSUMIC TECHNOLOGY CO., LTD. All rights reserved.
 *
 *  Redistribution and use in source and binary forms, with or without
 *  modification, are permitted provided that the following conditions
 *  are met:
 *    1. Redistributions of source code must retain the above copyright
 *       notice, this list of conditions and the following disclaimer.
 *    2. Redistributions in binary form must reproduce the above copyright
 *       notice, this list of conditions and the following disclaimer in the
 *       documentation and/or other materials provided with the
 *       distribution.
 *    3. Neither the name of POSSUMIC TECHNOLOGY CO., LTD. nor the names of
 *       its contributors may be used to endorse or promote products derived
 *       from this software without specific prior written permission.
 *
 *  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 *  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 *  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 *  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 *  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 *  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 *  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 *  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 *  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 *  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 *  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 **************************************************************************************************
 */

#include "r3_databox_msg_handler.h"
#include "mmw_ctrl.h"
#include "hif.h"
#include "mmw_app_pointcloud.h"
#include "mmw_point_cloud_psic_lib.h"
#include "mmw_app_pointcloud_config.h"
#include "r3_databox_2d_dsp.h"
#include "r3_databox_1d_dsp.h"
#include "r3_databox_debug_tool.h"
#include <math.h>
#include "mmw_report.h"
#if CONFIG_MMW_MICRO_POINT_CLOUD
#include "mmw_alg_doa.h"
#include "mmw_app_micro_pointcloud.h"
#endif

static bool s_read_current_config_flag = false;

/* 
 * The fisrt and second frame after call mmw_start() contains invalid data, so we should bypass 2st frame
 * This flag is set in r3_databox_startup_config() function, and count in r3_databox_1d/2d_frame_cb()
 */
static uint8_t s_r3_databox_skip_frame_status = R3_DATABOX_SKIP_FRAME_NUM;

DataBoxConfig_t g_databox_global_config = 
{
    .data_box_frame_config = 
    {
#if CONFIG_SOC_SERIES_RS624X
        .mimo_mode = MMW_MIMO_2T4R,
#elif CONFIG_SOC_SERIES_RS613X
        .mimo_mode = MMW_MIMO_1T3R,
#endif
        .frame_type = 1,
        .start_freq_mhz = 59000,
        .max_trigger_range_mm = 20480,
        .range_resolution_mm = 80,
        .max_range_rate_mm = 3200,
        .vel_resolution_mm = 200,
#if CONFIG_R3_DATA_BOX_UPLOAD_DATA_CUBE
#if CONFIG_BOARD_MRS6130_P1812 || CONFIG_SOC_SERIES_RS624X
		.frame_period_ms = 200
#elif CONFIG_BOARD_MRS6130_P1806
		.frame_period_ms = 2000
#endif
#else
		.frame_period_ms = 100
#endif
    }
    
};

int r3_databox_startup_config()
{
    int ret = 0;
    uint8_t mimo_mode = g_databox_global_config.data_box_frame_config.mimo_mode;
    if(g_databox_global_config.data_box_frame_config.frame_type == 0) {
        ret = mmw_mode_cfg(mimo_mode, MMW_WORK_MODE_1DFFT); /* config 1d work mode */
    } else if (g_databox_global_config.data_box_frame_config.frame_type == 1) {
        ret = mmw_mode_cfg(mimo_mode, MMW_WORK_MODE_2DFFT); /* config 2d work mode */
    } else {
        ret = 1;
    }
    if (ret) {
        LOG_PRINT("mode cfg error! %d\n", ret);
        return ret;
    }
    
    /* config start freq */
    ret = mmw_freq_cfg(g_databox_global_config.data_box_frame_config.start_freq_mhz, 0);
    if (ret) {
        LOG_PRINT("start freq cfg error! %d\n", ret);
        return ret;
    }
    
    /* config max det range and range res */
    ret = mmw_range_cfg(g_databox_global_config.data_box_frame_config.max_trigger_range_mm, g_databox_global_config.data_box_frame_config.range_resolution_mm);
    if (ret) {
        LOG_PRINT("range cfg error! %d\n", ret);
        return ret;
    }
    if(g_databox_global_config.data_box_frame_config.frame_type == 1) {
        /* config max velocity and velocity res */
        ret = mmw_velocity_cfg(g_databox_global_config.data_box_frame_config.max_range_rate_mm, g_databox_global_config.data_box_frame_config.vel_resolution_mm);
        if (ret) {
            LOG_PRINT("velocity cfg error! %d\n", ret);
            return ret;
        }
    }
    
	ret = mmw_chirp_num_cfg(1);
	if (ret) {
        LOG_PRINT("acc num cfg error! %d\n", ret);
        return ret;
    }
	
    /* config frame period */
    ret = mmw_frame_cfg(g_databox_global_config.data_box_frame_config.frame_period_ms, 0);
    if (ret) {
        LOG_PRINT("frame cfg error! %d\n", ret);
        return ret;
    }

    uint32_t frame_period = 0;
    uint32_t frame_num = 0;
    ret = mmw_frame_get(&frame_period, &frame_num);
    if (ret) {
        LOG_PRINT("frame get error! %d\n", ret);
        return ret;
    }
    frame_period = frame_period < 110 ? frame_period : 110;
    frame_period -= 10;
    HIF_ExtControl(HIF_SET_SEND_FRAME_TO, (void *)&frame_period, sizeof(frame_period));
	
	mmw_point_cloud_bb_config();

    /* accord frame type register callback function */
    if(g_databox_global_config.data_box_frame_config.frame_type == 0) {
        ret = mmw_ctrl_callback_cfg(&r3_databox_1d_frame_cb, MMW_DATA_TYPE_1DFFT, NULL);
    } else if (g_databox_global_config.data_box_frame_config.frame_type == 1) {
#if CONFIG_MMW_MICRO_POINT_CLOUD
        mmw_coordinate_config(MMW_COORDINATE_TYPE_CART);
        mmw_angle_mount_type_set(mmw_point_cloud_get_user_cfg_const()->mount_type);
        mmw_micro_point_init();
		mmw_micro_point_restart();
		/* Override: SDK sets clutter_rm_method=NONE when MICRO=1,
		   which kills motion point cloud CFAR. Force ALL to keep both. */
		mmw_point_cloud_get_user_cfg()->mmw_point_cloud_detection_config.clutter_rm_method = POINT_CLUTTER_REMOVAL_ALL;
#endif
        mmw_point_cloud_init(); /* init the para of point cloud process */
#if SW_CFAR_ENABLE
        mmw_fft_autogain_set(0); /* SW CFAR only support fix gain */
#endif
#if CONFIG_R3_DATA_BOX_UPLOAD_1DFFT_DATA
		ret = r3_databox_obtain_1d_fft_data_init();	//init range fft result data that need to be reported in 2d frame mode
#endif
		if (ret) {
			LOG_PRINT("obtain_1d_fft_data_ini fail %d\n", ret);
		}
		ret = mmw_ctrl_callback_cfg(&r3_databox_2d_frame_cb, MMW_DATA_TYPE_2DFFT, NULL);
	} else {
		ret = 1;
	}
	if (ret) {
        LOG_PRINT("mmw_ctrl_callback_cfg fail %d\n", ret);
    }
	
	r3_databox_reset_skip_frame_status(); /* reset the skipped frames status */
	r3_databox_reset_gain_factor_frame_idx(); /* reset the gain_factor_frame_idx */
	
    return 0;
}

static int r3_databox_start_ctrl_handler(HIF_MsgHdr_t *msg)
{
    int ret = HIF_CMD_STATUS_SUCCESS;

    uint8_t start = *((uint8_t *)(msg + 1));

    if(start) {
        ret = r3_databox_startup_config();
        mmw_report_param_get();
        ret = mmw_ctrl_start();
    } else {
        ret = mmw_sensor_stop();

        int mmw_state = 0;
        int to = 0;
        do {
            /* get mmw state, 4: runing */
            mmw_state = can_mmw_configured();
            if ((mmw_state != 4) && (mmw_event_process_completed())) {
                /* alreay stop */
                break;
            } else {
                /* prevent freezing, max 200ms */
                if (to >= 200) {
                    break;
                }
                OSI_MSleep(1);
            }
            to++;
        } while (1);

		if (g_databox_global_config.data_box_frame_config.frame_type == 1) {
			mmw_point_cloud_deinit();
#if CONFIG_R3_DATA_BOX_UPLOAD_1DFFT_DATA
			r3_databox_obtain_1d_fft_data_deinit();
#endif
#if CONFIG_MMW_MICRO_POINT_CLOUD
			mmw_micro_point_deinit();
#endif
		}
    }
    if(ret) {
        ret = HIF_CMD_STATUS_IO;
    }

    return HIF_MsgResp(msg, 0, ret);
}

static uint8_t para_check(uint32_t para, uint32_t min, uint32_t max)
{
    return (para >= min) && (para <= max);
}

static int r3_databox_single_cfg_handler(strMsgTlv *msgTlv)
{
    int ret = MMW_ERR_CODE_INVALID_PARAM;
    uint32_t tmp;
    switch(msgTlv->type) {
        case TLV_R3_DATA_BOX_CONFIG_MIMO_MODE:
        {
            #if CONFIG_SOC_SERIES_RS613X
            if (para_check(msgTlv->value[0], MMW_MIMO_1T3R, MMW_MIMO_1T3R)) {
                g_databox_global_config.data_box_frame_config.mimo_mode = msgTlv->value[0];       
                ret = 0;
            }
            #elif  CONFIG_SOC_SERIES_RS624X
            if (para_check(msgTlv->value[0], MMW_MIMO_2T4R, MMW_MIMO_2T4R)) {
                g_databox_global_config.data_box_frame_config.mimo_mode = msgTlv->value[0];                
                ret = 0;
            }
            #else
            #error "Please Choose Valid SOC Series"
            #endif
            break;
        }
        
        case TLV_R3_DATA_BOX_CONFIG_FRAME_TYPE:
        {
            if (para_check(msgTlv->value[0], 0, 1)) {
                g_databox_global_config.data_box_frame_config.frame_type = msgTlv->value[0];
                ret = 0;
            }
            break;
        }
        
        case TLV_R3_DATA_BOX_CONFIG_START_FREQ:
        {
            /* 1LSB = 1MHz */
            tmp = (msgTlv->value[1] << 8) + msgTlv->value[0];
            if (para_check(tmp, 57000, 64000)) {
                g_databox_global_config.data_box_frame_config.start_freq_mhz = tmp;
                ret = 0;
            }
            break;
        }
        
        case TLV_R3_DATA_BOX_CONFIG_TRIGGER_RANGE:
        {
            g_databox_global_config.data_box_frame_config.max_trigger_range_mm = (msgTlv->value[1] << 8) + msgTlv->value[0];
            ret = 0;
            break;
        }
        
        case TLV_R3_DATA_BOX_CONFIG_RANGE_RESOLUTION:
        {
            g_databox_global_config.data_box_frame_config.range_resolution_mm = (msgTlv->value[1] << 8) + msgTlv->value[0];
            ret = 0;
            break;
        }
        
        case TLV_R3_DATA_BOX_CONFIG_MAX_VELOCITY:
        {
            g_databox_global_config.data_box_frame_config.max_range_rate_mm = (msgTlv->value[1] << 8) + msgTlv->value[0];
            ret = 0;
            break;
        }
        
        case TLV_R3_DATA_BOX_CONFIG_VEL_RESOLUTION:
        {
            g_databox_global_config.data_box_frame_config.vel_resolution_mm = (msgTlv->value[1] << 8) + msgTlv->value[0];
            ret = 0;
            break;
        }
        case TLV_RE_DATA_BOX_CONFIG_FRAME_PERIOD:
        {
            g_databox_global_config.data_box_frame_config.frame_period_ms = (msgTlv->value[1] << 8) + msgTlv->value[0];
            ret = 0;
            break;            
        }
        default:
            ret = HIF_CMD_STATUS_UNSUPPORT;
            break;
    }

    return ret;
}

static int r3_databox_cfg_msg_handler(HIF_MsgHdr_t *msg)
{
    int ret = 0;
    uint16_t offset = 0;
    uint8_t *payload = (uint8_t *)(msg + 1);
    strMsgTlv *msgTlv = NULL;

    do{
        msgTlv = (strMsgTlv *)(&payload[offset]);
        ret = r3_databox_single_cfg_handler(msgTlv);
        if (ret != 0) {
            break;
        }
        offset += (msgTlv->len + 2);
    }while(offset < msg->length);

    return HIF_MsgResp(msg, 0, ret);
}

static int r3_databox_get_msg_handler(HIF_MsgHdr_t *msg)
{
    int ret = 0;
    strMsgTlv *msgTlv = (strMsgTlv *)(msg + 1);

    s_read_current_config_flag = true;
    ret = r3_databox_single_cfg_handler(msgTlv);
    s_read_current_config_flag = false;

    return HIF_MsgResp(msg, msgTlv->len+2 , ret);
}

int r3_databox_msg_init(void)
{
    int status = 0;

    status = HIF_MsgHdl_Regist(HIF_MSG_ID_START_CTRL, r3_databox_start_ctrl_handler);
    status = HIF_MsgHdl_Regist(HIF_MSG_ID_MOTION_SENSOR_PARA_CFG, r3_databox_cfg_msg_handler);
    status = HIF_MsgHdl_Regist(HIF_MSG_ID_MOTION_SENSOR_INFO_GET, r3_databox_get_msg_handler);

    return status;
}

uint8_t r3_databox_obtain_skip_frame_status(void) {
	return s_r3_databox_skip_frame_status;
}

void r3_databox_update_skip_frame_status(void) {
    s_r3_databox_skip_frame_status--;
}

void r3_databox_reset_skip_frame_status(void) {
    s_r3_databox_skip_frame_status = R3_DATABOX_SKIP_FRAME_NUM;
}