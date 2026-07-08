#include <math.h>
#include "mmw_ctrl.h"
#include "mmw_point_cloud_psic_lib.h"
#include "mmw_alg_pointcloud.h"
#include "mmw_app_pointcloud.h"
#include "mmw_alg_debug.h"
#include "log.h"
#include "r3_databox_2d_dsp.h"

static uint32_t s_databox_gain_factor_frame_idx = 0; /* used for host to check data validity */

void dbg_printk_config(void)
{
	int8_t ret = 0;
	
	uint8_t txrx_mode, work_mode;
	ret = mmw_mode_get(&txrx_mode, &work_mode);
	if (ret == 0) {
		LOG_PRINT("txrx_mode:%d,work_mode:%d\n",txrx_mode, work_mode);
	} else {
		LOG_PRINT("get mode config fail\n");
	}
	
	uint32_t range_mm, resol_mm;
	ret = mmw_range_get(&range_mm, &resol_mm);
	if (ret == 0) {
		LOG_PRINT("max_det_range_mm:%d,range_rel:%d\n",range_mm, resol_mm);
	} else {
		LOG_PRINT("get range config fail\n");
	}
	
	uint32_t velocity_mm, veloc_resol;
	ret = mmw_velocity_get (&velocity_mm, &veloc_resol);
	if (ret == 0) {
		LOG_PRINT("velocity_mm:%d,veloc_resol:%d\n",velocity_mm, veloc_resol);
	} else {
		LOG_PRINT("get velocity_mm config fail\n");
	}
	
	uint32_t period_ms, frame_num;
	ret = mmw_frame_get(&period_ms, &frame_num);
	if (ret == 0) {
		LOG_PRINT("period_ms:%d,frame_num:%d\n",period_ms, frame_num);
	} else {
		LOG_PRINT("get frame config fail\n");
	}
	
	uint32_t start_MHz, max_MHz;
	ret = mmw_freq_get(&start_MHz, &max_MHz);
	if (ret == 0) {
		LOG_PRINT("start_MHz:%d,max_MHz:%d\n",start_MHz, max_MHz);
	} else {
		LOG_PRINT("get freq config fail\n");
	}
	
	LOG_PRINT("\n");
}

#if (CONFIG_MMW_MICRO_POINT_CLOUD)
__sram_text void mmw_micro_point_cloud_upload(PointCloud3D *ptr_3d_mpc, uint16_t mpc_len){
	float *ptr_out = 0;
	float *buf_x = 0;
	char *name_buffer = 0;
	mmw_process_mem_alloc((void**)&name_buffer, sizeof(*name_buffer) * 50); /* if use non-blocking method,the name buffer needs to allocate memory in the heap */
	sprintf(name_buffer, "micro_point_cloud");        
	if (mpc_len) {
		mmw_process_mem_alloc((void**)&ptr_out, sizeof(*ptr_out) * mpc_len * 5);
		if (!ptr_out) {
			mmw_process_mem_free((void**)&name_buffer);
			return;
		}
		buf_x = ptr_out;
		for (uint16_t pc_idx = 0; pc_idx < mpc_len; pc_idx++) {
			*buf_x++ = (float)ptr_3d_mpc[pc_idx].cart.x;
			*buf_x++ = (float)ptr_3d_mpc[pc_idx].cart.y;
			*buf_x++ = (float)ptr_3d_mpc[pc_idx].cart.z;
			*buf_x++ = (float)ptr_3d_mpc[pc_idx].cart.vel;
			*buf_x++ = (float)ptr_3d_mpc[pc_idx].cart.snr;
		}
	}
	/* return 0 indicates that the HIF task has been added successfully,otherwise the memory needs to be released */
	if (mmw_psic_debug_proto_report_async(ptr_out, name_buffer, 5, mpc_len, PSIC_DBG_PROTO_DATA_FORMAT_FLOATING, PSIC_DBG_PROTO_DATA_F32, 0, 0, mmw_psic_debug_protocol_free_all_cb)) {
		mmw_process_mem_free((void**)&ptr_out);
		mmw_process_mem_free((void**)&name_buffer);
	}
}
#endif

/** 
 *	@brief  motion point cloud upload to RadarDebugTool
 * 			use API "mmw_psic_debug_proto_report_async" to upload point cloud data
 * 			the conversion of carti as follow:
 * 			X = range * sin_azi;
 * 		 	Y = range * sqrt(1 - sin_azi * sin_azi - sin_ele * sin_ele);
 * 			Z = range * sin_ele;
 * 			sin_azi, sin_ele is geted from the result of "mmw_point_cloud_process"
 * */
__sram_text void r3_databox_point_cloud_upload(const PointCloudBuffer_t *ptr_3d_pc, uint16_t pc_len)
{
	float range_cm;
	float sin_y;
	float *ptr_out = 0;
	float *buf_x = 0;
	uint32_t range_mm, range_reol_mm;
	uint32_t velocity_mm, veloc_resol;
	uint16_t range_fft_num, doppler_fft_num;
	float range_bin_size_cm;
	float dop_bin_size_cm, doppler_zero;
	char *name_buffer = 0;
	mmw_process_mem_alloc((void**)&name_buffer, sizeof(*name_buffer) * 50);
	sprintf(name_buffer, "motion_point_cloud");

	mmw_range_get(&range_mm, &range_reol_mm);
	mmw_fft_num_get(&range_fft_num, &doppler_fft_num);
	mmw_velocity_get(&velocity_mm, &veloc_resol);
	range_bin_size_cm = (float)range_mm / range_fft_num * 0.1f;
	dop_bin_size_cm = (float)velocity_mm / doppler_fft_num * 0.2f;
	doppler_zero = doppler_fft_num * 0.5f;
	if (pc_len) {
		mmw_process_mem_alloc((void**)&ptr_out, sizeof(*ptr_out) * pc_len * 5);
		if (!ptr_out) {
			return;
		}
		buf_x = ptr_out;
		for (uint16_t pc_idx = 0; pc_idx < pc_len; pc_idx++) {
			range_cm = ptr_3d_pc->ptr_motion_point_cloud_data[pc_idx].range_idx * range_bin_size_cm;
			sin_y = sqrtf(1 - ptr_3d_pc->ptr_motion_point_cloud_data[pc_idx].azi_phase * ptr_3d_pc->ptr_motion_point_cloud_data[pc_idx].azi_phase -
				ptr_3d_pc->ptr_motion_point_cloud_data[pc_idx].ele_phase * ptr_3d_pc->ptr_motion_point_cloud_data[pc_idx].ele_phase);
			mmw_point_cloud_trans_radar_coord_to_user_coord(
						range_cm * ptr_3d_pc->ptr_motion_point_cloud_data[pc_idx].azi_phase, /* x */
						range_cm * sin_y,                                                    /* y */
						range_cm * ptr_3d_pc->ptr_motion_point_cloud_data[pc_idx].ele_phase, /* z */
						buf_x
						);
			buf_x[3] = (ptr_3d_pc->ptr_motion_point_cloud_data[pc_idx].doppler_idx - doppler_zero) * dop_bin_size_cm;
			buf_x[4] = ptr_3d_pc->ptr_motion_point_cloud_data[pc_idx].sig_snr * 100.0f;
			buf_x += 5;
		}
	}
	if (mmw_psic_debug_proto_report_async(ptr_out, name_buffer, 5, pc_len, PSIC_DBG_PROTO_DATA_FORMAT_FLOATING, PSIC_DBG_PROTO_DATA_F32, 1, 0, mmw_psic_debug_protocol_free_all_cb)) {
		mmw_process_mem_free((void**)&ptr_out);
		mmw_process_mem_free((void**)&name_buffer);
	}
}
/**
 * @brief get gain factor for each rbin/tx_idx, and basic gain into fft_auto_gain_buff
 * @param fft_auto_gain_buff: save gain factor buffer
 * @param rfft_num: range FFT sample number.
 * @param mimo_info: pointer of tx ant num/rx ant num /mimo rx num.
 */
__sram_text void r3_databox_read_gain_factor(uint8_t *fft_auto_gain_buff, const MmwPsicMimoRxNum_t *mimo_info, uint16_t rfft_num)
{
    uint8_t *ptr_current_fft_gain;
	
	ptr_current_fft_gain = fft_auto_gain_buff;

	/* Get gain offset for each range bin and corresponding tx index */
    for (uint8_t tx_idx = 0; tx_idx < mimo_info->ant_tx_num; tx_idx++) {
        for (uint16_t rbin_idx = 0; rbin_idx < rfft_num; rbin_idx++) {
            *ptr_current_fft_gain= mmw_psic_auto_gain_rbin_get(rbin_idx, tx_idx);
            ptr_current_fft_gain++;
        }
    }

	/* Get basic gain for each tx */
    for (uint8_t tx_idx = 0; tx_idx < mimo_info->ant_tx_num; tx_idx++) {
        *ptr_current_fft_gain = mmw_psic_auto_gain_base_get(tx_idx);
        ptr_current_fft_gain++;
    }
}

/**
 * @brief Upload gain factor saved in fft_auto_gain_buff via HIF interface, together with frame index.
 * @param fft_auto_gain_buff: save gain factor buffer
 * @param range_fft_num: range FFT sample number.
 * @param mimo_info: pointer of tx ant num/rx ant num /mimo rx num.
 */
__sram_text void r3_databox_upload_gain_factor(uint8_t *fft_auto_gain_buff, uint16_t range_fft_num, const MmwPsicMimoRxNum_t *mimo_info)
{
    uint8_t *ptr_fft_auto_gain = 0;

    /* upload start flag of spectrum */
    if (range_fft_num > 0) {
        mmw_process_mem_alloc((void **)&ptr_fft_auto_gain, (range_fft_num * mimo_info->ant_tx_num + mimo_info->ant_tx_num + 4));
        memset(ptr_fft_auto_gain, 0, (range_fft_num * mimo_info->ant_tx_num + mimo_info->ant_tx_num + 4));
    }

	/* upload frame index (4bytes) */
	ptr_fft_auto_gain[0] = s_databox_gain_factor_frame_idx & 0xff;
	ptr_fft_auto_gain[1] = (s_databox_gain_factor_frame_idx >> 8) & 0xff;
	ptr_fft_auto_gain[2] = (s_databox_gain_factor_frame_idx >> 16) & 0xff;
	ptr_fft_auto_gain[3] = (s_databox_gain_factor_frame_idx >> 24) & 0xff;
	
	/* upload ptr_fft_gain_arr, each tx has different base gain, each range idx has own gain, */
    for (uint16_t rbin_idx = 0; rbin_idx < (range_fft_num * mimo_info->ant_tx_num + mimo_info->ant_tx_num); rbin_idx++) {
        ptr_fft_auto_gain[rbin_idx + 4] = fft_auto_gain_buff[rbin_idx];
    }
	
    char* ptr_sig_name = 0;
    mmw_process_mem_alloc((void**)&ptr_sig_name, 50);
    sprintf(ptr_sig_name, "gain factor");
	/* return 0 indicates that the HIF task has been added successfully,otherwise the memory needs to be released */
	if (mmw_psic_debug_proto_report_async((void*)ptr_fft_auto_gain, ptr_sig_name, 1, (range_fft_num * mimo_info->ant_tx_num + mimo_info->ant_tx_num + 4), PSIC_DBG_PROTO_DATA_FORMAT_BYTE,  PSIC_DBG_PROTO_DATA_UNSIGNED, 0, 0, mmw_psic_debug_protocol_free_all_cb)) {
		mmw_process_mem_free((void**)&ptr_fft_auto_gain);
		mmw_process_mem_free((void**)&ptr_sig_name);
	}
}

__sram_text void r3_databox_upload_gain_factor_process(void)
{
	/* Buffer to save auto gain for fixed-point data_cube upload.
	 * fft_auto_gain_buff have 3 parts sequencely in memory:
	 * 		  gain_offset_tx0: [0 ~ range_fft_num];
	 * 		  gain_offset_tx1: [0 ~ range_fft_num];
	 * 		  basic gain:      [basic_gain_tx0, basic_gain_tx1].
	 * float value for each range, e.g tx0:
	 *        data / power(2, gain_offset_tx0[range_idx] + basic_gain_tx0)
	 * */
	
	uint16_t rfft_num, dfft_num;
	MmwPsicMimoRxNum_t mimo_info;
	uint8_t *fft_auto_gain_buff = 0;
	
	mmw_fft_num_get(&rfft_num, &dfft_num);
    mmw_psic_lib_sdk_get_tx_rx_num(&mimo_info);
	
	/* allocate memory */
	if (fft_auto_gain_buff == 0) {
		mmw_process_mem_alloc((void**)&fft_auto_gain_buff, sizeof(*fft_auto_gain_buff) * (rfft_num * mimo_info.ant_tx_num + mimo_info.ant_tx_num));
	}
	
	r3_databox_read_gain_factor(fft_auto_gain_buff, &mimo_info, rfft_num);
	r3_databox_upload_gain_factor(fft_auto_gain_buff, rfft_num, &mimo_info);
	mmw_process_mem_free((void**)&fft_auto_gain_buff);
	s_databox_gain_factor_frame_idx++;
}

__sram_text void r3_databox_reset_gain_factor_frame_idx(void)
{
	s_databox_gain_factor_frame_idx = 0;
}

__sram_text void r3_databox_upload_zeros_dop_bin_data_async_hif_cb(hif_queue_item_t *hif_queue_item, int8_t status)
{
    Complex16_RealImag *ptr_data_header;
	MmwPsicMimoRxNum_t mimo_rx_info;
	uint16_t range_fft_num, doppler_fft_num;

	mmw_fft_num_get(&range_fft_num, &doppler_fft_num);
	mmw_psic_lib_sdk_get_tx_rx_num(&mimo_rx_info);
    
	payload_node_t *payload_head = hif_queue_item->payload;
	mmw_process_mem_free((void**)&payload_head->payload); /* first is header buffer */
	
	payload_head = payload_head->next;
	mmw_process_mem_free((void**)&payload_head->payload); /* second is name buffer */
    
	payload_head = payload_head->next;
    ptr_data_header = (Complex16_RealImag*)payload_head->payload; /* according to 3rd buffer to find initial address. */
    ptr_data_header = ptr_data_header - (mimo_rx_info.mimo_rx_num - 1) * range_fft_num;
	mmw_process_mem_free((void**)&ptr_data_header);
}

__sram_text void r3_databox_upload_zeros_dop_bin_data(void)
{
	
	uint16_t range_fft_num, doppler_fft_num, zeros_doppler_idx;

	MmwPsicMimoRxNum_t mimo_rx_info;

	Complex16_RealImag* ptr_range_spectrum[MMW_POINT_CLOUD_MAX_MIMO_RX_NUM];
	memset(ptr_range_spectrum, 0, MMW_POINT_CLOUD_MAX_MIMO_RX_NUM * sizeof(Complex16_RealImag*));

    int ant_idx = 0;
    int err_flag = 0;

	mmw_motion_cube_access_open();
	
	mmw_fft_num_get(&range_fft_num, &doppler_fft_num);
	zeros_doppler_idx = doppler_fft_num >> 1; /* get zeros dop bin index */
	
	mmw_psic_lib_sdk_get_tx_rx_num(&mimo_rx_info);
	
	char *name_buffer = 0;
	for (uint8_t tx_idx = 0; tx_idx < mimo_rx_info.ant_tx_num; tx_idx++) {
		for (uint8_t rx_idx = 0; rx_idx < mimo_rx_info.ant_rx_num; rx_idx++) {
            name_buffer = 0;
            mmw_process_mem_alloc((void**)&name_buffer, sizeof(*name_buffer) * 50); /* if use non-blocking method,the name buffer needs to allocate memory in the heap */
            sprintf(name_buffer, "zeros_dop_data_rx%d", ant_idx);
			mmw_process_mem_alloc((void **)&ptr_range_spectrum[ant_idx], sizeof(Complex16_RealImag) * range_fft_num);
			
			mmw_fft_range(ptr_range_spectrum[ant_idx], range_fft_num, tx_idx, rx_idx, zeros_doppler_idx);
			/* return 0 indicates that the HIF task has been added successfully,otherwise the memory needs to be released */
			if (mmw_psic_debug_proto_report_async((void*)ptr_range_spectrum[ant_idx], name_buffer, 1, range_fft_num * 2, PSIC_DBG_PROTO_DATA_FORMAT_SHORT, PSIC_DBG_PROTO_DATA_SIGNED, 0, 0, mmw_psic_debug_protocol_free_all_cb)) {
				mmw_process_mem_free((void**)&ptr_range_spectrum[ant_idx]);
				mmw_process_mem_free((void**)&name_buffer);
				err_flag = 1; /* if HIF task has been added failure,other ant data will no longer be transmitted */
				break;
			}
			ant_idx++;
        }
		if (err_flag) {
			break;
		}
    }
	mmw_motion_cube_access_close();
}

/*  the data arrangment methof ptr_range_spectrum: */
/*	chirp0:[rx0][range0],[rx0][range1],···,[rx0][range_max]
 * 		   [rx1][range0],[rx1][range1],···,[rx1][range_max]
 * 				·
 *				·
 *				·
 * 		   [rx_max][range0],[rx_max][range1],···,[rx_max][range_max]
 *  chirp1:	similar to chirp0,it goes to chirp_max	*/
__sram_text void r3_databox_upload_1d_fft_data(void)
{
	complex16_cube *ptr_range_spectrum = r3_databox_get_1d_fft_buffer(); /* get range fft data point that need to be reported */
	Databox1dFftDataConfig_t config_1d_fft_data = r3_databox_get_1d_fft_config(); /* get the range fft result config that need to be reported */
	int err_flag = 0;
	char *name_buffer = 0;
	for (uint8_t ant_idx = 0; ant_idx < config_1d_fft_data.config_mimo_rx_num; ant_idx++)
	{
		for(uint16_t dop_idx = 0; dop_idx < config_1d_fft_data.intv_num; dop_idx++)
		{
			name_buffer = 0;
			mmw_process_mem_alloc((void**)&name_buffer, sizeof(*name_buffer) * 50); /* if use non-blocking method,the name buffer needs to allocate memory in the heap */
			sprintf(name_buffer, "1d_data");
			/* return 0 indicates that the HIF task has been added successfully,otherwise the memory needs to be released */
			if (mmw_psic_debug_proto_report_async((void*)ptr_range_spectrum, name_buffer, 1, config_1d_fft_data.range_num * 2, PSIC_DBG_PROTO_DATA_FORMAT_SHORT, PSIC_DBG_PROTO_DATA_SIGNED, 0, 0, mmw_psic_debug_protocol_free_header_channel_name_cb)) {
				mmw_process_mem_free((void**)&name_buffer);
				err_flag = 1; /* if HIF task has been added failure,other ant data will no longer be transmitted */
				break;
			}
			ptr_range_spectrum += config_1d_fft_data.range_num; /* a packet of data is of range fft length */
		}
		if (err_flag) {
			break;
		}
	}
}