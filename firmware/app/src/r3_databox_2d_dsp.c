
#include "mmw_ctrl.h"
#include "r3_databox_debug_tool.h"
#include "mmw_point_cloud_psic_lib.h"
#include "mmw_app_pointcloud.h"
#include "mmw_alg_pointcloud.h"
#include "mmw_alg_debug.h"
#include "mmw_type.h"
#include "log.h"
#include "r3_databox_2d_dsp.h"
#include "mmw_report.h"
#include "r3_databox_msg_handler.h"
#if CONFIG_MMW_MICRO_POINT_CLOUD
#include "mmw_alg_doa.h"
#include "mmw_app_micro_pointcloud.h"
#endif

#if CONFIG_MMW_MICRO_POINT_CLOUD
typedef struct micro_report_arg {
	PointCloud3D *buffer;
	uint32_t  point_num;
	int32_t   micro_range_unit; /* cm-q4 */
	int32_t   micro_veloc_unit; /* cm/s Q7 */
} MmwMicroPointCloudOut_t;
#endif

Complex16_RealImag *g_1d_fft_data_buff = 0; /* store 1d fft data */
Databox1dFftDataConfig_t g_1d_fft_data_config; /* store 1d fft data config */

#if CONFIG_R3_DATA_BOX_UPLOAD_1DFFT_DATA
int r3_databox_obtain_1d_fft_data_init(void)
{
	int8_t ret = 0;

	uint16_t range_fft_num, doppler_fft_num;
	mmw_fft_num_get(&range_fft_num, &doppler_fft_num);
	/* config 1d data scope 1:0 - range_fft_num range bin 2:0 - 0 dop bin 3:all tx ants 4:all rx ants */
	g_1d_fft_data_config.range_start = 0;
	g_1d_fft_data_config.range_num = range_fft_num;
	g_1d_fft_data_config.start_intv = 0;
	g_1d_fft_data_config.end_intv = 0;
	g_1d_fft_data_config.tx_id = MMW_ANT_ID_ALL;
	g_1d_fft_data_config.rx_id = MMW_ANT_ID_ALL;
	ret |= mmw_2dfft_obtain_1dfft_cfg(g_1d_fft_data_config.range_start, g_1d_fft_data_config.range_num, g_1d_fft_data_config.start_intv, g_1d_fft_data_config.end_intv, g_1d_fft_data_config.tx_id, g_1d_fft_data_config.rx_id);
	g_1d_fft_data_config.intv_num = g_1d_fft_data_config.end_intv - g_1d_fft_data_config.start_intv + 1;

	uint8_t config_tx_num = 0;
	uint8_t config_rx_num = 0;
	MmwPsicMimoRxNum_t mimo_rx_info;
	mmw_psic_lib_sdk_get_tx_rx_num(&mimo_rx_info);
	if (g_1d_fft_data_config.tx_id == MMW_ANT_ID_ALL)
	{
		config_tx_num = mimo_rx_info.ant_tx_num;
	}
	if (g_1d_fft_data_config.rx_id == MMW_ANT_ID_ALL)
	{
		config_rx_num = mimo_rx_info.ant_rx_num;
	}
	g_1d_fft_data_config.config_mimo_rx_num = config_tx_num * config_rx_num;
	/* allocate the memory size according mmw_2dfft_obtain_1dfft_cfg */
	mmw_process_mem_alloc((void **)&g_1d_fft_data_buff, sizeof(Complex16_RealImag) * g_1d_fft_data_config.config_mimo_rx_num * g_1d_fft_data_config.intv_num * g_1d_fft_data_config.range_num);
	ret |= mmw_2dfft_set_1dfft_buffer(g_1d_fft_data_buff); /* regist pointer */
	return ret;
}

void r3_databox_obtain_1d_fft_data_deinit(void)
{
	mmw_process_mem_free((void**)&g_1d_fft_data_buff);
}
#endif

#if (CONFIG_MMW_MICRO_POINT_CLOUD)
/* Approximate 10*log10(snr_linear) → dB, same as analyse-spi mmw_report.c */
static uint16_t mpc_snr_trans_db(uint32_t snr_lin)
{
	uint16_t snr_db = 0;
	while (snr_lin >= 4) {
		snr_db += 3;
		snr_lin = snr_lin >> 1;
	}
	return snr_db + (snr_lin == 3 ? 5 : (snr_lin == 2 ? 3 : 0));
}

/**
 *	@brief  micro point cloud upload to RadarDebugTool
 *			if user use micro point cloud, enable macro "CONFIG_MMW_MICRO_POINT_CLOUD" in prj_config.h
 * 			carti is geted from the result of "mmw_micro_point_process"
 * 			the data type of carti is  int16
 * 			in "mmw_psic_debug_proto_report", use "PSIC_DBG_PROTO_DATA_FORMAT_SHORT" to upload micro point cloud
 * */
__sram_text int micro_point_cloud_data_handler(uint32_t range_idx, int veloc_idx,
					uint32_t snr_linear, MmwAngleInfo_t *angle, void *arg)
{
	MmwMicroPointCloudOut_t *report = (MmwMicroPointCloudOut_t *)arg;
	int32_t range_cm_q4 = range_idx * report->micro_range_unit;

    PointCloud_Cart *cart = (PointCloud_Cart *)report->buffer;
    int32_t range_cmq1 = (range_cm_q4 >> 3);
    mmw_micro_frame_trans_radar_coord_to_user_coord(
        (range_cmq1 * angle->sinValue_X) >> 16,
        (range_cmq1 * angle->sinValue_Y) >> 16,
        (range_cmq1 * angle->sinValue_Z) >> 16,
        &cart[report->point_num]
        );
	/* Fill velocity (cm/s Q7 to int16) and SNR for 5-column float32 output */
	cart[report->point_num].vel = (int16_t)((veloc_idx * report->micro_veloc_unit) >> 7);
	cart[report->point_num].snr = (int16_t)(mpc_snr_trans_db(snr_linear) * 100);

	return (++report->point_num >= MICRO_POINT_MAX);
}

#endif

__sram_text int r3_databox_2d_frame_cb(void *mmw_data, void *arg) {

	/* Skip default 2 frames to wait for clutter coverage */
	if (!r3_databox_obtain_skip_frame_status()) { /* return vaule of 0 indicates that the invalid data frames has skipped */
		mmw_dsp_poweron();
		PointCloudBuffer_t *ptr_point_cloud_buffer;

		r3_databox_upload_gain_factor_process(); /* upload fft gain if used auto gain */
		
#if CONFIG_R3_DATA_BOX_UPLOAD_1DFFT_DATA
		r3_databox_upload_1d_fft_data(); /* upload range fft result in 2d frame mode */
#endif

#if SW_CFAR_ENABLE
		ptr_point_cloud_buffer = mmw_point_cloud_process_sw_cfar();
#else
		ptr_point_cloud_buffer = mmw_point_cloud_process();
#endif

#if (CONFIG_MMW_MICRO_POINT_CLOUD)
		bool micro_update = mmw_micro_point_frame();

		/* auto gain function must be cleared manually as soon as gain factor is not used,
		   to make auto gain in next frame works well.
		   Hardware reuired gain factor cleared before chirp transmit period
		 */
		mmw_psic_auto_gain_clear();

		MmwMicroPointCloudOut_t mpc_output;
		uint16_t range_fft_num, doppler_fft_num;
		uint32_t range_mm, range_reol_mm;
		mmw_range_get(&range_mm, &range_reol_mm);
		mmw_fft_num_get(&range_fft_num, &doppler_fft_num);

		mpc_output.micro_range_unit = (range_mm << 4) / 10 / range_fft_num;
		{
			uint32_t _fp, _dummy;
			mmw_frame_get(&_fp, &_dummy);
			_fp = _fp * mmw_micro_frame_rate_get();
			mpc_output.micro_veloc_unit = (5 * 100 << 7) / (_fp * mmw_micro_doppler_num_get());
		}
		mpc_output.point_num = 0;
		mpc_output.buffer = 0;
		mmw_process_mem_alloc((void**)&mpc_output.buffer, MICRO_POINT_MAX * sizeof(*mpc_output.buffer));
		/* mmw frames down sampling for micro frame.
		 * During down sampling period, MCU keeps storing micro frame, ‘micro_update’ will keep ‘0’,
		 * therefor process micro points when new micro frame is ready(‘micro_update’ = 1).
		 * */
		if (micro_update) {
			mmw_micro_point_process(micro_point_cloud_data_handler, (void*)&mpc_output);
			mmw_micro_point_cloud_upload(mpc_output.buffer, mpc_output.point_num);
		}
		mmw_process_mem_free((void**)&mpc_output.buffer);
#else
		/* auto gain function must be cleared manually as soon as gain factor is not used,
		   to make auto gain in next frame works well.
		   Hardware reuired gain factor cleared before chirp transmit period
		 */
		mmw_psic_auto_gain_clear();
#endif

		/* TODO: Add user prorcess here */


		r3_databox_point_cloud_upload(ptr_point_cloud_buffer, ptr_point_cloud_buffer->point_cloud_num);
		mmw_process_mem_free((void**) &ptr_point_cloud_buffer->ptr_motion_point_cloud_data);
		mmw_process_mem_free((void**) &ptr_point_cloud_buffer);

		if ((mmw_point_cloud_get_user_cfg_const()->mmw_point_cloud_detection_config.clutter_rm_method == POINT_CLUTTER_REMOVAL_DC) && 
			(!mmw_point_cloud_get_user_cfg_const()->mmw_point_cloud_detection_config.clutter_halt_en)) {
			mmw_psic_dc_suppression_update();
		}
#if CONFIG_R3_DATA_BOX_UPLOAD_DATA_CUBE
		uint32_t com_type = 0;
		HIF_ParamGet(HIF_SET_COM_TYPE, &com_type, sizeof(com_type));
		if(com_type == HIF_COM_TYPE_SPI) {
			mmw_ctrl_data_cube_spi_report_cb(mmw_data, arg);
		} else if(com_type == HIF_COM_TYPE_UART){
			mmw_ctrl_data_cube_uart_report_cb(mmw_data, arg);
		}
#endif
	}else {
		r3_databox_update_skip_frame_status(); /* count skipped frames */
		/* when clutter_rm_method is DC,skip the last frame init DC */
		if (mmw_point_cloud_get_user_cfg_const()->mmw_point_cloud_detection_config.clutter_rm_method == POINT_CLUTTER_REMOVAL_DC && !r3_databox_obtain_skip_frame_status()) { 
            mmw_clutter_halt_set(MMW_HALT_CLUTTER_UPDATE_ENABLE);
			mmw_psic_dc_suppression_init();
			if (!mmw_point_cloud_get_user_cfg_const()->mmw_point_cloud_detection_config.clutter_halt_en) {
				/* mmw_psic_dc_suppression_init will clear cluuter and needs to be update once */
				mmw_psic_dc_suppression_update();
			}
        }
		/* auto gain function must be cleared manually as soon as gain factor is not used,
		   to make auto gain in next frame works well.
		   Hardware reuired gain factor cleared before chirp transmit period
		 */
		mmw_psic_auto_gain_clear();
	}
    return 0;
}

__sram_text Complex16_RealImag *r3_databox_get_1d_fft_buffer(void)
{
	return g_1d_fft_data_buff;
}

__sram_text Databox1dFftDataConfig_t r3_databox_get_1d_fft_config(void)
{
	return g_1d_fft_data_config;
}
